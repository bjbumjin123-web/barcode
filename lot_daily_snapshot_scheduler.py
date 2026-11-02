# -*- coding: utf-8 -*-
"""
LOT 일일 동기화 스케줄러 (Supabase 버전) - 검은 창 실행용 .py 한 파일 (재고 + 자재입고 LOT + 생산실적 LOT)
- 이 창이 떠 있으면 스케줄러가 작동 중이고, 창을 닫으면 종료됩니다. (예전 .pyw 는 창이 없어 꺼졌는지 알 수 없었음)
- 실행하면 이미 돌고 있던 예전 스케줄러(.pyw 포함)는 자동으로 종료됩니다.
- 07:58, 19:58 (20일~익월5일은 07:00 추가) 에 실행: 재고 -> 자재입고 -> 생산실적 순서, 하나가 실패해도 나머지는 계속
    1) 재고      : MES(WIPL_LOTSTK_SEL08) -> Supabase 'stock'
    2) 자재입고  : MES(MWMSWHGLOT)         -> Supabase 'lot_master' (LOT/품번/품명/날짜/처음 수량)
    3) 생산실적  : MES(MWIPRESDAT)         -> Supabase 'lot_master' (LOT/품번/품명/날짜/처음 수량)
- 실패 시 5분 간격으로 최대 6회 재시도, 결과는 lot_daily_snapshot_log.txt 에 기록
- 실행 시각이 지났는데 성공 기록이 없으면 시작 직후 한 번 따라잡기 실행 (끄려면 CATCH_UP = False)
- 5분마다 작동 상태 한 줄을 표시하고, 웹 '갱신현황' 화면에도 '작동 중' 신호를 올립니다.

사용법(검은 창에서):
  python lot_daily_snapshot_scheduler.py                 그냥 시작 (더블클릭도 가능) - 대기하며 자동 실행
  python lot_daily_snapshot_scheduler.py --now           지금 재고+자재+생산을 한 번 실행한 뒤 계속 대기
  python lot_daily_snapshot_scheduler.py --now mat       지금 특정 작업만 실행 (stock / mat / prod) 후 계속 대기
  python lot_daily_snapshot_scheduler.py --now --once    지금 실행하고 대기 없이 종료
  python lot_daily_snapshot_scheduler.py --params        프로시저 매개변수 목록 확인
  python lot_daily_snapshot_scheduler.py --probe         재고 MES 결과 컬럼명/샘플 확인 (저장 안 함)
  python lot_daily_snapshot_scheduler.py --probe-lot     자재/생산 LOT 조회 샘플 확인 (저장 안 함)
  python lot_daily_snapshot_scheduler.py --check 2609290016-001 [LOT ...]   LOT 의 처음 수량(생산/입고 기록) 확인 - 잔량 판단용 (조회만)
  python lot_daily_snapshot_scheduler.py --backfill --dry   LOT 처음 수량 채우기 미리보기 (최근 90일, 저장 안 함)
  python lot_daily_snapshot_scheduler.py --backfill         LOT 처음 수량 채우기 (최초 1회, 최근 90일) / 기간 변경: --days 60
"""
import os
import sys
import json
import time
import datetime
import subprocess
import urllib.request
import urllib.parse
import urllib.error

# ==========================================
# 설정
# ==========================================
CATCH_UP = True                         
MAX_ATTEMPTS = 6
RETRY_SEC = 300
LOT_DAYS_BACK = 3                       

# MES
MSSQL_SERVER = '192.168.0.115'
MSSQL_DATABASE = 'DSIMES_BJM'
MSSQL_UID = 'bumjin'
MSSQL_PWD = 'lq[ALX[Fu_A6Q*Qb'

# Supabase
SUPABASE_URL = 'https://siwaynipvwtwuoobyarp.supabase.co'
SUPABASE_KEY = 'sb_publishable_lEKhT8TYzo368Hxfd0E8-Q_D9JEyO9Z'

# 서버 전용 비밀 키: 같은 폴더의 supabase_secret.txt (또는 환경변수 SUPABASE_SECRET_KEY)에 한 줄로 넣어 두면 그 키를 사용합니다.
# 파일이 없으면 위의 기존 키로 동작합니다. (비밀 키 파일은 절대 GitHub에 올리지 마세요)
def _load_secret_key():
    k = os.environ.get('SUPABASE_SECRET_KEY', '').strip()
    if not k:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'supabase_secret.txt'), encoding='utf-8') as f:
                k = f.read().strip()
        except Exception:
            k = ''
    return k
_SECRET = _load_secret_key()
USING_SECRET_KEY = bool(_SECRET)
if _SECRET:
    SUPABASE_KEY = _SECRET
TABLE = 'stock'
LOT_TABLE = 'lot_master'

PROC_ARGS = [''] * 8 + ['KO']
QUERY_TIMEOUT_SEC = 180

COLUMN_CANDIDATES = {
    'lot_no':    ['LOT_NO'],
    'item_no':   ['MAT_CD'],
    'item_name': ['MAT_NM'],
    'qty':       ['LOT_QTY', 'CUR_QTY', 'STOCK_QTY'],
    'unit':      ['UNIT_CD', 'UNIT_NM'],
    'warehouse': ['STO_NM', 'WH_NM'],
    'ref':       ['MAT_BIGO'],      
}

# 재고 프로시저(WIPL_LOTSTK_SEL08) 결과에서 LOT 상태 등을 가져와 stock 테이블의 같은 이름 컬럼에 저장합니다.
#   저장컬럼       : [프로시저 결과 컬럼명 후보 - 앞에서부터 값이 있는 것 사용]
# 비워 두면({}) 가져오지 않습니다. Supabase 에 컬럼이 아직 없으면 그 컬럼만 빼고 저장하고 로그에 알립니다.
# (컬럼 추가: 4_stock_lot_status.sql, 6_stock_status_columns.sql)
STOCK_EXTRA_COLUMNS = {
    'lot_status':    ['LOT_STS_NM'],      # LOT 상태: 실적생성 / 투입취소 / 창고이동 / 자재투입 / 수입검사 / 외주완료 입고 / 재고실사 / 검사포장 / 발행 / 입고
    'lot_substatus': ['LOT_SUBSTS_NM'],   # 세부 상태: LOT분리 / 투입소모 / 외주출고 / 불출 ...
    'chk_type':      ['CHK_TYPE'],        # 실사 구분: MES 데이터 / PDA 스캔 / 실사불가 상태
}

# ORIGIN_QTY = 이 LOT 의 '처음 수량' (잔량 판단용). 조회 기간에 기록이 있는 LOT 은 전체 기간의 수량을 합산함.
SQL_LOT_MAT = """
    SELECT LTRIM(RTRIM(A.LOT_NO)) AS LOT_NO, A.MAT_CD, B.MAT_NM, MAX(A.WHG_DAY) AS WORK_DAY,
           SUM(ISNULL(A.WHG_QTY, 0)) AS ORIGIN_QTY
    FROM DSIMES_BJM.dbo.MWMSWHGLOT A
    LEFT JOIN DSIMES_BJM.dbo.MBASMATMST B ON A.MAT_CD = B.MAT_CD
    WHERE ISNULL(A.CANCEL_YN, 'N') <> 'Y'
      AND A.LOT_NO IN (
            SELECT X.LOT_NO FROM DSIMES_BJM.dbo.MWMSWHGLOT X
            WHERE ISNULL(X.CANCEL_YN, 'N') <> 'Y' AND X.WHG_DAY BETWEEN ? AND ?)
      AND A.LOT_NO IS NOT NULL AND LTRIM(RTRIM(A.LOT_NO)) <> ''
    GROUP BY LTRIM(RTRIM(A.LOT_NO)), A.MAT_CD, B.MAT_NM
"""
SQL_LOT_PROD = """
    SELECT LTRIM(RTRIM(A.LOT_NO)) AS LOT_NO, A.MAT_CD, B.MAT_NM, MAX(A.WORK_DAY) AS WORK_DAY,
           SUM(ISNULL(A.LOT_QTY, 0)) AS ORIGIN_QTY
    FROM DSIMES_BJM.dbo.MWIPRESDAT A
    LEFT JOIN DSIMES_BJM.dbo.MBASMATMST B ON A.MAT_CD = B.MAT_CD
    WHERE A.LOT_NO IN (
            SELECT X.LOT_NO FROM DSIMES_BJM.dbo.MWIPRESDAT X
            WHERE X.WORK_DAY BETWEEN ? AND ?)
      AND A.LOT_NO IS NOT NULL AND LTRIM(RTRIM(A.LOT_NO)) <> ''
    GROUP BY LTRIM(RTRIM(A.LOT_NO)), A.MAT_CD, B.MAT_NM
"""

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, 'lot_daily_snapshot_log.txt')
PID_FILE = os.path.join(BASE_DIR, 'scheduler.pid')
STATE_FILE = os.path.join(BASE_DIR, 'lot_daily_sync_state.txt')      
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S')

def log(msg):
    line = f'[{now_str()}] {msg}'
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    try:
        if sys.stdout:
            print(line)
    except Exception:
        pass

# ==========================================
# 중복 실행 방지
# ==========================================
def stop_previous_instance():
    old = None
    try:
        with open(PID_FILE) as f:
            old = int(f.read().strip())
    except Exception:
        pass
    me = os.getpid()
    if old and old != me:
        try:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {old}', '/FO', 'CSV', '/NH'],
                                 capture_output=True, creationflags=NO_WINDOW).stdout.lower()
            if b'python' in out:
                subprocess.run(['taskkill', '/F', '/PID', str(old)],
                               capture_output=True, creationflags=NO_WINDOW)
                log(f'이전에 실행 중이던 스케줄러(PID {old})를 종료했습니다.')
        except Exception as e:
            log(f'이전 프로세스 종료 확인 중 오류: {e}')
    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(me))
    except Exception:
        pass

# ==========================================
# 상태 관리 (시간 슬롯 포함)
# ==========================================
def load_state():
    today = time.strftime('%Y-%m-%d')
    done = set()
    try:
        with open(STATE_FILE) as f:
            for line in f.read().split('\n'):
                parts = line.strip().split(' ')
                if len(parts) == 3 and parts[0] == today:
                    done.add((parts[1], parts[2]))
    except Exception:
        pass
    return done

def save_state(done):
    today = time.strftime('%Y-%m-%d')
    try:
        with open(STATE_FILE, 'w') as f:
            f.write('\n'.join(f'{today} {slot} {t}' for slot, t in sorted(done)))
    except Exception:
        pass

# ==========================================
# MES 조회 및 Supabase 업로드
# ==========================================
def get_mssql_connection():
    import pyodbc
    available = pyodbc.drivers()
    preferred = [
        'ODBC Driver 18 for SQL Server',
        'ODBC Driver 17 for SQL Server',
        'ODBC Driver 13 for SQL Server',
        'SQL Server Native Client 11.0',
        'SQL Server',
    ]
    selected = next((d for d in preferred if d in available), None)
    if not selected:
        raise Exception(f'PC에 사용 가능한 MSSQL ODBC 드라이버가 없습니다. (현재: {available})')
    conn_str = (f'DRIVER={{{selected}}};SERVER={MSSQL_SERVER};DATABASE={MSSQL_DATABASE};'
                f'UID={MSSQL_UID};PWD={MSSQL_PWD};')
    if '17' in selected or '18' in selected:
        conn_str += 'TrustServerCertificate=yes;'
    return pyodbc.connect(conn_str, timeout=30)

def query_mes():
    conn = get_mssql_connection()
    try:
        conn.timeout = QUERY_TIMEOUT_SEC
        cur = conn.cursor()
        cur.execute('EXEC WIPL_LOTSTK_SEL08 ' + ', '.join('?' * len(PROC_ARGS)), PROC_ARGS)
        while cur.description is None:      
            if not cur.nextset():
                raise Exception('프로시저가 결과셋을 반환하지 않았습니다.')
        cols = [c[0].upper() for c in cur.description]
        return cols, cur.fetchall()
    finally:
        conn.close()

def show_params():
    conn = get_mssql_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT PARAMETER_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, PARAMETER_MODE "
                    "FROM INFORMATION_SCHEMA.PARAMETERS WHERE SPECIFIC_NAME = 'WIPL_LOTSTK_SEL08' "
                    "ORDER BY ORDINAL_POSITION")
        print('WIPL_LOTSTK_SEL08 매개변수:')
        for r in cur.fetchall():
            print('  ', tuple(r))
    finally:
        conn.close()

def pick(rowdict, field):
    for name in COLUMN_CANDIDATES[field]:
        if name in rowdict and rowdict[name] is not None:
            return str(rowdict[name]).strip()
    return ''

def to_int(v):
    try:
        return int(float(v))          
    except Exception:
        return 0

def classify_department(ref, warehouse, item_name=''):
    """부서 분류. 비고(ref)에 2F/주조/사상/1F 가 있으면 그쪽이 먼저.
    반제품창고: 품명에 '주조' -> 주조 (단 품명에 '창고'도 같이 있으면 1F)
               1F 로 분류된 것 중 비고에 '청우' -> 청우 부서
    (※ upload.html 의 classifyDepartment 와 같은 규칙 - 한쪽을 고치면 다른 쪽도 고칠 것)"""
    ref = ref or ''
    warehouse = warehouse or ''
    item_name = item_name or ''
    wh = ''.join(warehouse.split())          # 공백 무시 ('반제품 창고' = '반제품창고')
    is_cw = '청우' in ref
    if warehouse == '원자재창고':
        return '자재', None
    if warehouse == '완제품창고':
        return '영업', None
    if warehouse == '외주창고':
        return '외주', None
    if '2F' in ref:
        return '2F(검사/포장)', None
    if '주조' in ref or '사상' in ref:
        return '주조', None
    dept = None
    if '1F' in ref:
        dept = '1F(가공생산)'
    elif '반제품' in warehouse:
        if '주조' in item_name and '창고' not in item_name:
            return '주조', None
        dept = '1F(가공생산)'
    if dept:
        if wh == '반제품창고' and is_cw:
            return '청우', None
        return dept, ('청우' if is_cw else '자체')
    return '미분류', None

def build_docs(cols, rows, upload_date, run_at):
    by_lot = {}
    for row in rows:
        d = {c: v for c, v in zip(cols, row)}
        lot_no = pick(d, 'lot_no')
        if not lot_no:
            continue
        warehouse = pick(d, 'warehouse')
        dept, sub = classify_department(pick(d, 'ref'), warehouse, pick(d, 'item_name'))
        by_lot[lot_no] = {
            'lot_no': lot_no,
            'item_no': pick(d, 'item_no'),
            'item_name': pick(d, 'item_name'),
            'qty': to_int(pick(d, 'qty')),
            'unit': pick(d, 'unit'),
            'warehouse': warehouse,
            'department': dept,
            'subcontract_type': sub,
            'upload_date': upload_date,
            'uploaded_at': run_at,
            'run_at': run_at,
        }
        for key, names in STOCK_EXTRA_COLUMNS.items():      # 모든 행에 같은 키로 넣음 (일괄 저장은 키가 같아야 안전)
            val = ''
            for name in names:
                if name in d and d[name] is not None and str(d[name]).strip():
                    val = str(d[name]).strip()
                    break
            by_lot[lot_no][key] = val or None
    return list(by_lot.values())

def fmt_day(v):
    s = ''.join(ch for ch in str(v or '') if ch.isdigit())[:8]
    if len(s) != 8:
        return ''
    return f'{s[0:4]}-{s[4:6]}-{s[6:8]}'

def query_lot_rows(sql, label, s=None, e=None):
    if not s or not e:      # 기간을 따로 주지 않으면 최근 LOT_DAYS_BACK 일
        end = datetime.datetime.now()
        start = end - datetime.timedelta(days=max(LOT_DAYS_BACK - 1, 0))
        s, e = start.strftime('%Y%m%d'), end.strftime('%Y%m%d')
    conn = get_mssql_connection()
    try:
        conn.timeout = QUERY_TIMEOUT_SEC
        cur = conn.cursor()
        cur.execute(sql, (s, e))
        rows = cur.fetchall()
    finally:
        conn.close()
    log(f'  [{label}] 조회 기간 {s} ~ {e} - MES {len(rows)}건')
    return rows

def to_num(v):
    """수량(Decimal 등) -> JSON 으로 보낼 수 있는 숫자. 없거나 0 이하이면 None (처음 수량 정보 없음으로 처리)"""
    try:
        n = round(float(v), 3)
    except Exception:
        return None
    if n <= 0:
        return None
    return int(n) if n == int(n) else n

def build_lot_docs(rows, source):
    by_lot = {}
    for r in rows:
        lot_no = str(r[0] or '').strip()
        item_no = str(r[1] or '').strip()
        item_name = str(r[2] or '').strip()
        work_date = fmt_day(r[3])
        if not lot_no or not work_date or not (item_no or item_name):
            continue
        old = by_lot.get(lot_no)
        if old is None or work_date >= old['work_date']:      
            by_lot[lot_no] = {
                'lot_no': lot_no, 'item_no': item_no, 'item_name': item_name,
                'work_date': work_date, 'source': source,
                'origin_qty': to_num(r[4]) if len(r) > 4 else None,
            }
    return list(by_lot.values())

def run_lot(sql, label, source, s=None, e=None):
    rows = query_lot_rows(sql, label, s, e)
    docs = build_lot_docs(rows, source)
    if docs:
        upload_supabase(docs, LOT_TABLE, 'lot_no')
    return len(docs)

def run_mat():
    return run_lot(SQL_LOT_MAT, '자재입고', '자재')

def run_prod():
    return run_lot(SQL_LOT_PROD, '생산실적', '생산')

def upload_supabase(docs, table=TABLE, on_conflict='upload_date,lot_no,run_at'):
    url = f'{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}'
    headers = {
        'apikey': SUPABASE_KEY,
        'Content-Type': 'application/json',
        'Prefer': 'resolution=merge-duplicates,return=minimal',
    }
    if SUPABASE_KEY.startswith('eyJ'):
        headers['Authorization'] = 'Bearer ' + SUPABASE_KEY
    for i in range(0, len(docs), 500):
        body = json.dumps(docs[i:i + 500], ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='ignore')[:300]
            raise RuntimeError(f'Supabase HTTP {e.code}: {detail}')

CLOSE_TABLE = 'close_log'
ROUND_START_HOUR = 7         # 하루(1회) 기준: 07:00 ~ 다음날 06:59 를 한 회로 봄 (같은 회 안에서 재고가 다시 올라와도 마감 기록 안 함)

def round_date(s):
    """'YYYY-MM-DD HH:MM:SS' -> 그 시각이 속한 회차 날짜 (07:00 이전이면 전날)"""
    return (datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S') - datetime.timedelta(hours=ROUND_START_HOUR)).strftime('%Y-%m-%d')

def _rest_headers():
    h = {'apikey': SUPABASE_KEY}
    if SUPABASE_KEY.startswith('eyJ'):
        h['Authorization'] = 'Bearer ' + SUPABASE_KEY
    return h

def rest_get(table, query):
    req = urllib.request.Request(f'{SUPABASE_URL}/rest/v1/{table}?{query}', headers=_rest_headers())
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))

def rest_get_all(table, query):
    out, offset = [], 0
    while True:
        part = rest_get(table, f'{query}&limit=1000&offset={offset}')
        out.extend(part)
        if len(part) < 1000:
            return out
        offset += 1000

def rest_insert(table, docs):
    h = _rest_headers(); h['Content-Type'] = 'application/json'; h['Prefer'] = 'return=minimal'
    body = json.dumps(docs, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(f'{SUPABASE_URL}/rest/v1/{table}', data=body, headers=h, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Supabase HTTP {e.code}: ' + e.read().decode('utf-8', errors='ignore')[:200])

def auto_close_previous_round(new_run_at):
    """새 재고를 저장하기 직전에, 직전 회차의 마감 결과(총/확인/남음)를 부서별로 close_log 에 한 번만 자동 기록.
    확인 = 직전 회차(07:00~06:59)에 수량 입력까지 끝낸 LOT"""
    latest = rest_get('stock', 'select=run_at&order=upload_date.desc,run_at.desc&limit=1')
    if not latest or not latest[0].get('run_at'):
        return 0
    prev_run = latest[0]['run_at']
    prev_round, new_round = round_date(prev_run), round_date(new_run_at)
    if prev_round == new_round:
        return 0                                   # 같은 회차(07:00~06:59) 안의 재저장 -> 마감 아님
    q = urllib.parse.quote(prev_run)
    if rest_get('close_log', f'select=id&event=eq.auto_close&closed_at=gt.{q}&limit=1'):
        return 0                                   # 이미 기록됨 (재시도 중복 방지)
    prev_rows = rest_get_all('stock', f'select=lot_no,department&run_at=eq.{q}&order=id.asc')
    scans = rest_get_all('scan_log', f'select=lot_no&actual_qty=not.is.null&scan_date=eq.{prev_round}&order=id.asc')   # 직전 회차(scan_date=회차 날짜)에 수량 입력까지 끝낸 LOT
    done = {r['lot_no'] for r in scans}
    by_dept = {}
    for r in prev_rows:
        t = by_dept.setdefault(r.get('department') or '미분류', [0, 0])
        t[0] += 1
        if r['lot_no'] in done:
            t[1] += 1
    items = sorted(by_dept.items()) + [('전체', [sum(v[0] for v in by_dept.values()), sum(v[1] for v in by_dept.values())])]
    docs = [{'event': 'auto_close', 'scanner': '자동', 'dept': d, 'scan_date': prev_round, 'closed_at': new_run_at,
             'total': t, 'checked': c, 'remaining': t - c} for d, (t, c) in items]
    rest_insert(CLOSE_TABLE, docs)
    return len(docs)

STATUS_TABLE = 'sync_status'
LAST_RESULT = {}          # 검은 창(.py) 표시용: {작업키: (시각, 성공여부)}

def short_reason(e):
    """웹에 보여줄 짧은 실패 사유 (오류 원문은 로그 파일에만 남김)"""
    t = str(e)
    if 'Supabase HTTP' in t:
        return '저장 실패(' + t.split(':')[0].replace('Supabase ', '') + ')'
    if '0건' in t:
        return 'MES 재고 0건'
    low = t.lower()
    if 'odbc' in low or 'sql server' in low or 'timeout' in low or 'connect' in low:
        return 'MES 조회 실패'
    return '실행 오류'

def report_status(task, ok=None, count=None, message=None, success=False):
    """웹 '갱신현황' 화면용 상태 기록 (실패해도 본 작업에는 영향 없음)"""
    try:
        now = now_str()
        doc = {'task': task, 'last_attempt_at': now}
        if ok is not None:
            doc['ok'] = ok
        if count is not None:
            doc['row_count'] = count
        if message is not None:
            doc['message'] = message[:60]
        if success:
            doc['last_success_at'] = now
        upload_supabase([doc], STATUS_TABLE, 'task')
    except Exception:
        pass

_last_hb = 0
def heartbeat(note=''):
    """5분마다 '작동 중' 신호를 남김 -> 웹에서 스케줄러가 꺼졌는지 확인 가능"""
    global _last_hb
    if time.time() - _last_hb < 300:
        return
    _last_hb = time.time()
    report_status('scheduler', True, None, ('작동 중 ' + note).strip())

def upload_stock_docs(docs):
    """재고 저장. Supabase stock 에 상태 컬럼이 아직 없으면 (PostgREST: Could not find the 'xxx' column)
    그 컬럼만 빼고 다시 저장해서 재고 갱신 자체가 막히지 않게 함."""
    import re
    for _ in range(len(STOCK_EXTRA_COLUMNS) + 1):
        try:
            upload_supabase(docs)
            return
        except RuntimeError as e:
            msg = str(e)
            if not STOCK_EXTRA_COLUMNS or ('PGRST204' not in msg and 'column' not in msg.lower()):
                raise
            missing = [k for k in re.findall(r"'(\w+)' column", msg) if k in STOCK_EXTRA_COLUMNS]
            if not missing:
                missing = [k for k in STOCK_EXTRA_COLUMNS if k in docs[0]]   # 어떤 컬럼인지 모르면 상태 컬럼 전부 제외
            if not missing:
                raise
            for d in docs:
                for k in missing:
                    d.pop(k, None)
            log(f'stock 테이블에 컬럼이 없어 {missing} 를 빼고 저장합니다. (SQL 로 컬럼을 추가하세요)')
    raise RuntimeError('재고 저장 재시도 한도를 넘었습니다.')

def run_stock():
    cols, rows = query_mes()
    if not rows:
        raise Exception('MES 재고 조회 결과가 0건입니다. (저장하지 않음)')
    upload_date = time.strftime('%Y-%m-%d')
    run_at = now_str()
    docs = build_docs(cols, rows, upload_date, run_at)
    if not docs:
        raise Exception(f'LOT 번호 컬럼을 찾지 못했습니다. 조회된 컬럼: {cols}')
    try:
        n_close = auto_close_previous_round(run_at)
        if n_close:
            log(f'직전 회차 자동 마감 기록 {n_close}건 저장')
    except Exception as e:
        log(f'자동 마감 기록 실패(재고 저장은 계속 진행) - {e}')
    upload_stock_docs(docs)
    return len(docs)

TASKS = [
    ('stock', '재고', run_stock),
    ('mat', '자재입고 LOT', run_mat),
    ('prod', '생산실적 LOT', run_prod),
]

def run_task_with_retry(key, label, fn):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            n = fn()
            log(f'[{label}] 성공 ({attempt}/{MAX_ATTEMPTS}번째 시도) - {n}건 저장')
            LAST_RESULT[key] = (now_str(), True)
            report_status(key, True, n, '정상', success=True)
            return True
        except Exception as e:
            log(f'[{label}] 실패 ({attempt}/{MAX_ATTEMPTS}번째 시도) - {e}')
            LAST_RESULT[key] = (now_str(), False)
            report_status(key, False, None, short_reason(e))
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_SEC)
    log(f'[{label}] 재시도 모두 실패 - 다음 스케줄에 시도합니다.')
    return False

# ==========================================
# 다중 스케줄 감지 및 실행 (변경 적용 부분)
# ==========================================
def schedule_times(d):
    times = ['07:58', '19:58']
    if d.day >= 20 or d.day <= 5:
        times.append('07:00')
    return sorted(times)

def next_slot(now):
    """다음 실행 예정 (datetime, 'HH:MM')"""
    for day_offset in (0, 1):
        d = now + datetime.timedelta(days=day_offset)
        for t in schedule_times(d):
            cand = datetime.datetime.strptime(d.strftime('%Y-%m-%d') + ' ' + t, '%Y-%m-%d %H:%M')
            if cand > now:
                return cand, t
    return None, ''

def get_current_schedule_slot(now):
    times = schedule_times(now)
    hhmm = now.strftime('%H:%M')
    passed = [t for t in times if hhmm >= t]
    return passed[-1] if passed else None

def schedule_loop(on_tick=None):
    tried = set()          
    while True:
        try:
            now = datetime.datetime.now()
            today = now.strftime('%Y-%m-%d')
            hhmm = now.strftime('%H:%M')
            
            nxt, nxt_t = next_slot(now)
            heartbeat('(다음 ' + nxt_t + ')' if nxt_t else '')
            if on_tick:
                on_tick(now, nxt)

            slot = get_current_schedule_slot(now)
            if slot:
                done = load_state()
                pending = [t for t in TASKS if (slot, t[0]) not in done and (today, slot, t[0]) not in tried]
                
                if pending:
                    for key, _, _ in pending:
                        tried.add((today, slot, key))
                    
                    on_time = (hhmm == slot)
                    if on_time or CATCH_UP:
                        if on_time:
                            log(f'[{slot}] 스케줄 도달 - 실행 시작')
                        else:
                            log(f'[{slot}] 스케줄이 지났으나 오늘 성공 기록이 없어 즉시 실행 (따라잡기)')
                        
                        for key, label, fn in pending:
                            if run_task_with_retry(key, label, fn):
                                done.add((slot, key))
                                save_state(done)
            time.sleep(20)
        except Exception as e:
            log(f"스케줄러 에러: {e}")
            time.sleep(60)

def probe():
    cols, rows = query_mes()
    print('컬럼:', cols)
    print('총 행 수:', len(rows))
    for r in rows[:3]:
        print({c: v for c, v in zip(cols, r)})
    docs = build_docs(cols, rows[:3], time.strftime('%Y-%m-%d'), now_str())
    print('\n변환 결과 샘플(저장 안 함):')
    for d in docs:
        print(d)
    missing = [k for k in COLUMN_CANDIDATES if not any(n in cols for n in COLUMN_CANDIDATES[k])]
    if missing:
        print('\n[주의] 컬럼을 찾지 못한 항목:', missing, '-> COLUMN_CANDIDATES 에 실제 컬럼명을 추가하세요.')

    # LOT 상태로 쓸 만한 컬럼 찾기: 이름에 상태/구분/여부를 뜻하는 말이 들어 있는 컬럼의 값 종류와 건수
    hints = ('STS', 'STAT', 'FLAG', '_YN', 'HOLD', 'QC', 'INSP', 'BAD', 'GUBN', 'GUBUN', 'TP', 'TYPE', 'DIV')
    used = {n for names in COLUMN_CANDIDATES.values() for n in names}
    cands = [c for c in cols if any(h in c for h in hints) and c not in used]
    print('\n[LOT 상태 후보 컬럼] (이름에 상태/구분/여부 관련 글자가 들어 있는 컬럼)')
    if not cands:
        print('  후보 없음 - 전체 컬럼 목록을 보고 상태에 해당하는 컬럼을 알려 주세요.')
    for c in cands:
        i = cols.index(c)
        cnt = {}
        for r in rows:
            k = '' if r[i] is None else str(r[i]).strip()
            cnt[k] = cnt.get(k, 0) + 1
        top = sorted(cnt.items(), key=lambda x: -x[1])[:10]
        print(f'  {c}: ' + ', '.join(f'{k or "(빈값)"}={n}' for k, n in top) + (' ...' if len(cnt) > 10 else ''))
    if STOCK_EXTRA_COLUMNS:
        print('\n[STOCK_EXTRA_COLUMNS] 저장컬럼 <- 프로시저 컬럼')
        for k, names in STOCK_EXTRA_COLUMNS.items():
            print(f'  {k} <- {names}  (결과에 있음: {[c for c in names if c in cols]})')

def probe_lot():
    for sql, label, source in ((SQL_LOT_MAT, '자재입고', '자재'), (SQL_LOT_PROD, '생산실적', '생산')):
        rows = query_lot_rows(sql, label)
        docs = build_lot_docs(rows, source)
        print(f'[{label}] LOT {len(docs)}건 (저장 안 함) 샘플:')
        for d in docs[:5]:
            print('  ', d)

# ==========================================
# 검은 창 표시
# ==========================================
_last_print = 0


def fmt_remain(delta):
    m = int(delta.total_seconds() // 60)
    return f'{m // 60}시간 {m % 60}분'


def last_summary():
    if not LAST_RESULT:
        return '아직 실행 없음'
    parts = []
    for key, label, _ in TASKS:
        if key in LAST_RESULT:
            t, ok = LAST_RESULT[key]
            parts.append(f'{label} {"성공" if ok else "실패"} {t[11:16]}')
    return ' / '.join(parts)


def on_tick(now, nxt):
    """스케줄 루프가 돌 때마다 호출 - 5분마다 상태 한 줄 출력"""
    global _last_print
    if time.time() - _last_print < 300:
        return
    _last_print = time.time()
    nxt_txt = f'{nxt.strftime("%m-%d %H:%M")} (앞으로 {fmt_remain(nxt - now)})' if nxt else '-'
    print(f'[{now.strftime("%H:%M:%S")}] 🟢 작동 중 | 다음 실행: {nxt_txt} | 마지막: {last_summary()}')


def run_now(only=None):
    ok_all = True
    for key, label, fn in TASKS:
        if only and only != key:
            continue
        print(f'  ▶ {label} 조회·저장 중... (MES 응답에 따라 몇 분 걸릴 수 있습니다)')
        try:
            n = fn()
            log(f'[{label}] 성공 (수동 실행) - {n}건 저장')
            LAST_RESULT[key] = (now_str(), True)
            report_status(key, True, n, '정상(수동)', success=True)
            # 현재 실행 시각대(슬롯)를 완료로 기록 -> 직후에 '따라잡기'로 또 실행되지 않게 함
            slot = get_current_schedule_slot(datetime.datetime.now())
            if slot:
                done = load_state()
                done.add((slot, key))
                save_state(done)
            print(f'    ✅ {n}건 저장')
        except Exception as e:
            ok_all = False
            log(f'[{label}] 실패 (수동 실행) - {e}')
            LAST_RESULT[key] = (now_str(), False)
            report_status(key, False, None, short_reason(e))
            print(f'    ❌ 실패: {e}')
    return ok_all


# ==========================================
# 확인·채우기 도구 (예전 lot_origin_check.py / lot_origin_qty_backfill.py 를 이 파일로 통합)
# ==========================================
SQL_ORIGIN_PRD = """
SELECT A.MAT_CD, MAX(B.MAT_NM), SUM(ISNULL(A.LOT_QTY, 0)), COUNT(*), MIN(A.WORK_DAY), MAX(A.WORK_DAY)
FROM DSIMES_BJM.dbo.MWIPRESDAT A
LEFT JOIN DSIMES_BJM.dbo.MBASMATMST B ON A.MAT_CD = B.MAT_CD
WHERE A.LOT_NO = ?
GROUP BY A.MAT_CD
"""
SQL_ORIGIN_WHG = """
SELECT A.MAT_CD, MAX(B.MAT_NM), SUM(ISNULL(A.WHG_QTY, 0)), COUNT(*), MIN(A.WHG_DAY), MAX(A.WHG_DAY)
FROM DSIMES_BJM.dbo.MWMSWHGLOT A
LEFT JOIN DSIMES_BJM.dbo.MBASMATMST B ON A.MAT_CD = B.MAT_CD
WHERE A.CANCEL_YN <> 'Y' AND A.LOT_NO = ?
GROUP BY A.MAT_CD
"""
BACKFILL_CHUNK_DAYS = 10     # MES 타임아웃 방지: 기간을 나눠서 조회


def check_origin(lots):
    """LOT 의 '처음 수량'을 생산/입고 기록에서 보여줌 (조회만, 저장 안 함)"""
    conn = get_mssql_connection()
    try:
        cur = conn.cursor()
        for lot in lots:
            print(f'\n■ {lot}')
            for title, sql in (('생산', SQL_ORIGIN_PRD), ('입고', SQL_ORIGIN_WHG)):
                cur.execute(sql, (lot,))
                rows = cur.fetchall()
                if not rows:
                    print(f'   [{title}] 기록 없음')
                for r in rows:
                    print(f'   [{title}] 품번 {r[0]} | 품명 {r[1]} | 처음 수량 합계 {r[2]} | {r[3]}건 | {r[4]} ~ {r[5]}')
    finally:
        conn.close()


def backfill_origin(days_back=90, dry=False):
    """lot_master 의 처음 수량(origin_qty)을 최근 days_back 일치 한 번에 채움 (최초 1회용)"""
    end = datetime.datetime.now()
    cur_day = end - datetime.timedelta(days=days_back)
    chunks = []
    while cur_day <= end:
        nxt = min(cur_day + datetime.timedelta(days=BACKFILL_CHUNK_DAYS - 1), end)
        chunks.append((cur_day.strftime('%Y%m%d'), nxt.strftime('%Y%m%d')))
        cur_day = nxt + datetime.timedelta(days=1)
    print(f'LOT 처음 수량 채우기 - 최근 {days_back}일 {"(확인만, 저장 안 함)" if dry else ""}')
    total = 0
    for s, e in chunks:
        for sql, label, source in ((SQL_LOT_MAT, '자재입고', '자재'), (SQL_LOT_PROD, '생산실적', '생산')):
            docs = build_lot_docs(query_lot_rows(sql, label, s, e), source)
            with_qty = sum(1 for d in docs if d.get('origin_qty'))
            print(f'  [{label}] {s}~{e}: LOT {len(docs)}건 (처음 수량 있음 {with_qty})')
            if docs and not dry:
                upload_supabase(docs, LOT_TABLE, 'lot_no')
            total += len(docs)
    print(f'\n완료: 총 {total}건 {"확인" if dry else "저장"}')


def main():
    argv = sys.argv
    if '--params' in argv:
        show_params()
        return
    if '--probe' in argv:
        probe()
        return
    if '--probe-lot' in argv:
        probe_lot()
        return

    if '--check' in argv:
        i = argv.index('--check')
        lots = []
        for a in argv[i + 1:]:
            if a.startswith('--'):
                break
            lots.append(a.strip())
        if not lots:
            print('사용법: python lot_daily_snapshot_scheduler.py --check LOT번호 [LOT번호 ...]')
            return
        check_origin(lots)
        return
    if '--backfill' in argv:
        days = 90
        if '--days' in argv:
            days = int(argv[argv.index('--days') + 1])
        backfill_origin(days, dry='--dry' in argv)
        return

    run_flag = '--now' in argv
    only = None
    if run_flag:
        i = argv.index('--now')
        if i + 1 < len(argv) and not argv[i + 1].startswith('--'):
            only = argv[i + 1]
        if only and only not in [t[0] for t in TASKS]:
            print(f'알 수 없는 작업 이름: {only} (stock / mat / prod 중 하나)')
            return
    if run_flag and '--once' in argv:       # 한 번 실행하고 종료
        print('\n✅ 완료되었습니다.' if run_now(only) else '\n❌ 일부 실패 - lot_daily_snapshot_log.txt 를 확인하세요.')
        return

    stop_previous_instance()
    if os.name == 'nt':
        os.system('title LOT 재고 스케줄러 (이 창을 닫으면 종료)')
    now = datetime.datetime.now()
    print('\n==================================================')
    print(' 🕒 LOT 재고 자동 업데이트 스케줄러 (이 창을 닫으면 종료)')
    print(f' 오늘 실행 시각 : {", ".join(schedule_times(now))}')
    print(f' 저장 키        : {"서버 전용 키" if USING_SECRET_KEY else "기존 키 (supabase_secret.txt 없음)"}')
    print('==================================================')
    log('스케줄러 시작 (검은 창) - ' + ('서버 전용 키 사용' if USING_SECRET_KEY else '기존 키 사용'))

    if run_flag:
        print('\n(--now) 지금 즉시 업데이트를 실행합니다.\n')
        print('✅ 업데이트가 완료되었습니다.' if run_now(only) else '❌ 일부 실패 - lot_daily_snapshot_log.txt 를 확인하세요.')

    # 놓친 실행 시각이 있으면 스케줄러가 자동으로 따라잡기 실행합니다.
    print('\n[*] 자동 업데이트 대기 중입니다. 5분마다 상태가 표시됩니다.')
    print('[*] 종료하시려면 이 검은 창을 닫아주세요.\n')
    schedule_loop(on_tick)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n종료합니다.')
    except Exception as e:
        log(f'치명적 오류: {e}')
        import traceback
        traceback.print_exc()
        try:
            input('\n오류가 났습니다. 내용을 확인하신 뒤 엔터를 누르면 창이 닫힙니다...')
        except EOFError:
            pass
