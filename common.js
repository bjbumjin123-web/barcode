// LOT 스캐너 공통 모듈 (여러 화면이 함께 사용) - 메뉴/접속 정보/도우미를 여기서 한 번만 관리합니다.
import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";

export const SUPABASE_URL = "https://siwaynipvwtwuoobyarp.supabase.co";
export const SUPABASE_KEY = "sb_publishable_lEKhT8TYzo368Hxfd0E8-Q_D9JEyO9Z";
export const sb = createClient(SUPABASE_URL, SUPABASE_KEY);

// ★ 메뉴는 여기만 고치면 모든 화면에 반영됩니다. (id 는 각 화면이 renderNavbar 에 넘기는 값)
export const MENU = [
    { id: "index",    href: "index.html",    label: "📸 스캔" },
    { id: "scanview", href: "scanview.html", label: "🕒 기록조회" },
    { id: "round",    href: "round.html",    label: "📋 실사현황" },
    // ↓ round.html(실사현황)이 진행률+차이를 합친 화면입니다. 확인이 끝나면 아래 두 줄을 지우세요.
    { id: "progress", href: "progress.html", label: "📋 진행률" },
    { id: "diff",     href: "diff.html",     label: "⚖️ 차이" },
    { id: "history",  href: "history.html",  label: "📅 이력조회" },
    { id: "status",   href: "status.html",   label: "📊 갱신현황" },
];

// 현재 화면(currentId)을 뺀 메뉴 링크를 .navbar 안에 그림
export function renderNavbar(currentId, selector = ".navbar") {
    const el = document.querySelector(selector);
    if (!el) return;
    el.innerHTML = MENU.filter(m => m.id !== currentId).map(m => `<a href="${m.href}">${m.label}</a>`).join("");
}

export const DEPARTMENTS = ["1F(가공생산)", "2F(검사/포장)", "미분류", "영업", "외주", "자재", "주조", "청우"];

// d.data() 형태로 읽을 수 있게 감싸는 도우미 (기존 코드 호환용)
export const wrap = r => ({ id: r.id, data: () => r });

// Supabase 는 한 번에 1000행까지만 주므로 나눠서 전부 가져옴
export async function fetchAll(build) {
    const out = [];
    for (let from = 0; ; from += 1000) {
        const { data, error } = await build().order("id").range(from, from + 999);
        if (error) throw error;
        data.forEach(r => out.push(wrap(r)));
        if (data.length < 1000) break;
    }
    return out;
}

export function esc(s) {
    if (s === null || s === undefined) return "";
    return s.toString().replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// 한국시간 'YYYY-MM-DD HH:MM:SS' (스케줄러가 저장하는 시각 형식과 동일)
export function kstNow() {
    const p = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date());
    return p.replace("T", " ");
}
export const todayKST = () => kstNow().slice(0, 10);

// ===== 실사 회차(날짜) 도우미: 진행률 / 기록조회 / 차이조회가 함께 사용 =====
// 하루(1회) 기준: 한국시간 07:00 ~ 다음날 06:59 를 한 회차로 봄 -> 07:00 이전이면 전날 날짜가 '회차 날짜'
export const ROUND_START_HOUR = 7;
export function roundDateKST() {
    const fmt = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" });
    return fmt.format(new Date(Date.now() - ROUND_START_HOUR * 3600 * 1000));
}
const _utc = s => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
export function addDays(dateStr, n) {
    return new Date(_utc(dateStr) + n * 86400000).toISOString().slice(0, 10);
}
export function relDayText(date, today = roundDateKST()) {
    const n = Math.round((_utc(today) - _utc(date)) / 86400000);
    return n === 0 ? "오늘" : n === 1 ? "어제" : n > 0 ? `${n}일 전` : `${-n}일 후`;
}

// 실사 기록이 있는 날짜 목록 (최근 순). 오늘 회차는 기록이 없어도 항상 맨 위에 포함.
// 날짜별 건수는 Supabase 뷰 scan_date_counts 에서 가져오고, 뷰가 없으면 scan_log 에서 직접 집계(최근 14일)함.
export async function loadScanDates(days = 365) {
    const today = roundDateKST();
    let list = [];
    try {
        const { data, error } = await sb.from("scan_date_counts").select("*").gte("scan_date", addDays(today, -days));
        if (error) throw error;
        list = (data || []).map(r => ({ date: r.scan_date, scans: Number(r.scans) || 0, entries: Number(r.entries) || 0, lots: Number(r.lots) || 0, entryLots: Number(r.entry_lots) || 0, lastAt: r.last_at || "" }));
    } catch (e) {
        console.error("scan_date_counts 조회 실패 → scan_log 에서 직접 집계(최근 14일)", e);
        try {
            const snap = await fetchAll(() => sb.from("scan_log").select("scan_date,lot_no,actual_qty,scanned_at").gte("scan_date", addDays(today, -14)));
            const m = {};
            snap.forEach(d => {
                const r = d.data(); if (!r.scan_date) return;
                const x = m[r.scan_date] || (m[r.scan_date] = { date: r.scan_date, scans: 0, entries: 0, lotSet: new Set(), entrySet: new Set(), lastAt: "" });
                x.scans++; x.lotSet.add(r.lot_no);
                if (r.actual_qty !== null && r.actual_qty !== undefined) { x.entries++; x.entrySet.add(r.lot_no); }
                if ((r.scanned_at || "") > x.lastAt) x.lastAt = r.scanned_at;
            });
            list = Object.values(m).map(x => ({ date: x.date, scans: x.scans, entries: x.entries, lots: x.lotSet.size, entryLots: x.entrySet.size, lastAt: x.lastAt }));
        } catch (e2) { console.error(e2); }
    }
    if (!list.some(x => x.date === today)) list.push({ date: today, scans: 0, entries: 0, lots: 0, entryLots: 0, lastAt: "" });
    list.sort((a, b) => (a.date < b.date ? 1 : -1));
    return list;
}

// 날짜 선택: '년·월' 선택창 + '일' 선택창 두 단계로 나눠서 보여줌 (기록이 쌓여도 목록이 길어지지 않게)
// - 화면들은 기존처럼 sel(일 선택창)의 value / change 이벤트만 쓰면 됨. 년·월 선택창은 이 함수가 sel 바로 앞에 한 번만 만들어 재사용함.
// - 년·월을 바꾸면 그 달의 가장 최근 날짜가 선택되고 sel 에 change 이벤트가 발생함 (화면이 알아서 다시 조회).
// - 기록 없는 날짜는 숨김(오늘/선택한 날짜는 유지).
// mode: "entry"(수량 입력 건수 기준 - 진행률/차이조회) / "scan"(스캔 횟수 기준 - 기록조회)
export function fillDateSelect(sel, dates, selected, mode = "entry") {
    const today = roundDateKST();
    const list = dates.slice();
    if (selected && !list.some(x => x.date === selected)) list.push({ date: selected, scans: 0, entries: 0, lots: 0, entryLots: 0, lastAt: "" });
    list.sort((a, b) => (a.date < b.date ? 1 : -1));
    const items = [];
    list.forEach(x => {
        const n = mode === "scan" ? x.scans : x.entries, lots = mode === "scan" ? x.lots : x.entryLots;
        if (!n && x.date !== today && x.date !== selected) return;   // 기록 없는 날짜는 숨김
        items.push({ date: x.date, n, lots });
    });
    sel._dateItems = { items, mode, today };

    let msel = sel._monthSel;
    if (!msel) {
        msel = document.createElement("select");
        msel.id = (sel.id || "date") + "Month";
        msel.title = "년·월";
        sel.parentNode.insertBefore(msel, sel);
        sel._monthSel = msel;
        msel.addEventListener("change", () => { fillDayOptions(sel, msel.value); sel.dispatchEvent(new Event("change")); });
    }
    msel.innerHTML = "";
    const seen = new Set();
    items.forEach(it => {
        const ym = it.date.slice(0, 7);
        if (seen.has(ym)) return;
        seen.add(ym);
        const o = document.createElement("option");
        o.value = ym;
        o.textContent = `${ym.slice(0, 4)}년 ${+ym.slice(5, 7)}월`;
        msel.appendChild(o);
    });
    const target = selected || today, ym = target.slice(0, 7);
    msel.value = seen.has(ym) ? ym : (msel.options.length ? msel.options[0].value : "");
    fillDayOptions(sel, msel.value, target);
}
function fillDayOptions(sel, ym, pick) {
    const { items, mode, today } = sel._dateItems;
    sel.innerHTML = "";
    items.filter(it => it.date.slice(0, 7) === ym).forEach(it => {
        const o = document.createElement("option");
        o.value = it.date;
        o.textContent = `${it.date.slice(5)} (${relDayText(it.date, today)}) · ` + (it.n ? (mode === "scan" ? `스캔 ${it.n}회` : `입력 ${it.n}건`) + ` · LOT ${it.lots}개` : "기록 없음");
        sel.appendChild(o);
    });
    if (pick && Array.from(sel.options).some(o => o.value === pick)) sel.value = pick;
    else if (sel.options.length) sel.selectedIndex = 0;
}

// 선택한 회차에 맞는 재고를 가져옴: 그 날짜(upload_date)의 재고 중 가장 최근 실행분.
// 그 날짜 재고가 없으면 가장 가까운 이전 날짜 재고를 쓰고(exact=false), 아무것도 없으면 uploadDate=null (재고는 100일 지나면 삭제됨)
export async function loadStockForRound(date) {
    const { data: top, error } = await sb.from("stock").select("upload_date").lte("upload_date", date).order("upload_date", { ascending: false }).limit(1);
    if (error) throw error;
    if (!top || !top.length) return { uploadDate: null, runAt: "", exact: false, rows: [], byLot: {} };
    const uploadDate = top[0].upload_date;
    const snap = await fetchAll(() => sb.from("stock").select("*").eq("upload_date", uploadDate));
    const key = r => r.run_at || r.uploaded_at || "";
    const byLot = {};   // LOT 별로 그날 가장 최근 실행값
    snap.forEach(d => { const r = d.data(), ex = byLot[r.lot_no]; if (!ex || key(r) > key(ex)) byLot[r.lot_no] = r; });
    let runAt = ""; Object.values(byLot).forEach(r => { if (key(r) > runAt) runAt = key(r); });
    const rows = runAt ? Object.values(byLot).filter(r => key(r) === runAt) : Object.values(byLot);   // 그날 마지막 실행분에 있는 LOT 만
    return { uploadDate, runAt, exact: uploadDate === date, rows, byLot };
}

// 화면 위에 보여줄 '비교한 재고' 안내 문구 (⚠️ 로 시작하면 경고)
export function stockInfoText(date, st) {
    if (!st || !st.uploadDate) return "⚠️ 이 날짜에 비교할 재고가 없습니다 (재고는 100일이 지나면 삭제됩니다)";
    const at = (st.runAt || st.uploadDate).slice(5, 16);
    return st.exact ? `📦 비교한 재고: ${at} 실행분` : `⚠️ ${date} 재고가 없어 ${at} 실행분과 비교합니다`;
}
