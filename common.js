// LOT 스캐너 공통 모듈 (여러 화면이 함께 사용) - 메뉴/접속 정보/도우미를 여기서 한 번만 관리합니다.
import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";

export const SUPABASE_URL = "https://siwaynipvwtwuoobyarp.supabase.co";
export const SUPABASE_KEY = "sb_publishable_lEKhT8TYzo368Hxfd0E8-Q_D9JEyO9Z";
export const sb = createClient(SUPABASE_URL, SUPABASE_KEY);

// ★ 메뉴는 여기만 고치면 모든 화면에 반영됩니다. (id 는 각 화면이 renderNavbar 에 넘기는 값)
export const MENU = [
    { id: "index",    href: "index.html",    label: "📸 스캔" },
    { id: "scanview", href: "scanview.html", label: "🕒 기록조회" },
    { id: "progress", href: "progress.html", label: "📋 진행률" },
    { id: "history",  href: "history.html",  label: "📅 이력조회" },
    { id: "status",   href: "status.html",   label: "📊 갱신현황" },
];

// 현재 화면(currentId)을 뺀 메뉴 링크를 .navbar 안에 그림
export function renderNavbar(currentId, selector = ".navbar") {
    const el = document.querySelector(selector);
    if (!el) return;
    el.innerHTML = MENU.filter(m => m.id !== currentId).map(m => `<a href="${m.href}">${m.label}</a>`).join("");
}

export const DEPARTMENTS = ["1F(가공생산)", "2F(검사/포장)", "미분류", "영업", "외주", "자재", "주조"];

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
