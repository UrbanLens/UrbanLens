/**
 * The trip page's calendar view: which months it spans and the grid markup for them.
 */

import { escHtml } from "./escape-html";

export interface CalendarActivity {
    id: string;
    /** ``YYYY-MM-DD``. */
    date: string;
    title: string;
    index: string | null;
    status: string;
    moveUrl: string;
}

export interface CalendarMonth {
    y: number;
    /** 0-based. */
    m: number;
}

/** A calendar never spans more months than this, however long the trip. */
const MAX_MONTHS = 18;

function normalise(y: number, m: number): CalendarMonth {
    return { y: y + Math.floor(m / 12), m: ((m % 12) + 12) % 12 };
}

function parseYearMonth(iso: string): CalendarMonth | null {
    const [y, m] = iso.split("-").map((part) => Number.parseInt(part, 10));
    return y !== undefined && m !== undefined && !Number.isNaN(y) && !Number.isNaN(m) ? { y, m: m - 1 } : null;
}

/**
 * The months from the trip's start to its end, both shifted by ``offset``.
 *
 * Args:
 *     tripStart: ``YYYY-MM-DD``, or empty for the current month.
 *     tripEnd: ``YYYY-MM-DD``, or empty for the start month.
 *     offset: Months the viewer has paged forward (positive) or back.
 *     today: The current date.
 */
export function tripMonths(tripStart: string, tripEnd: string, offset: number, today: Date): CalendarMonth[] {
    const start = parseYearMonth(tripStart) ?? { y: today.getFullYear(), m: today.getMonth() };
    const end = parseYearMonth(tripEnd) ?? start;
    const first = normalise(start.y, start.m + offset);
    const last = normalise(end.y, end.m + offset);

    const months: CalendarMonth[] = [];
    let { y, m } = first;
    while ((y < last.y || (y === last.y && m <= last.m)) && months.length < MAX_MONTHS) {
        months.push({ y, m });
        m++;
        if (m > 11) {
            m = 0;
            y++;
        }
    }
    return months.length ? months : [first];
}

/** Which activities a tab shows: Completed shows only completed ones, every other tab hides them. */
export function activitiesForTab(activities: CalendarActivity[], tab: string): CalendarActivity[] {
    if (tab === "completed") return activities.filter((a) => a.status === "completed");
    return activities.filter((a) => a.status !== "completed" && (tab === "upcoming" || a.status === tab));
}

function isoDay(y: number, m: number, d: number): string {
    return `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

function monthLabel(month: CalendarMonth): string {
    return new Date(month.y, month.m, 1).toLocaleString("default", { month: "long", year: "numeric" });
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/**
 * The calendar grid for ``months``, with each activity on its day.
 *
 * Args:
 *     months: From :func:`tripMonths`; never empty.
 *     activities: Already filtered to the active tab.
 *     today: Highlighted when it falls in range.
 */
export function calendarHtml(months: CalendarMonth[], activities: CalendarActivity[], today: Date): string {
    const byDate = new Map<string, CalendarActivity[]>();
    for (const activity of activities) {
        if (!activity.date) continue;
        byDate.set(activity.date, [...(byDate.get(activity.date) ?? []), activity]);
    }
    const todayStr = isoDay(today.getFullYear(), today.getMonth(), today.getDate());

    const firstMonth = months[0] ?? { y: today.getFullYear(), m: today.getMonth() };
    const lastMonth = months[months.length - 1] ?? firstMonth;
    const navTitle = months.length > 1 ? `${monthLabel(firstMonth)} - ${monthLabel(lastMonth)}` : monthLabel(firstMonth);

    let html =
        '<div class="cal-header">' +
        '<button type="button" class="cal-nav-btn" data-trip-action="cal-nav" data-delta="-1" aria-label="Previous"><i class="material-symbols-outlined">chevron_left</i></button>' +
        `<span class="cal-month-label">${escHtml(navTitle)}</span>` +
        '<button type="button" class="cal-nav-btn" data-trip-action="cal-nav" data-delta="1" aria-label="Next"><i class="material-symbols-outlined">chevron_right</i></button>' +
        "</div>";

    months.forEach((month, idx) => {
        if (months.length > 1) {
            if (idx > 0) html += '<div class="cal-month-section-sep"></div>';
            html += `<div class="cal-month-section-label">${escHtml(monthLabel(month))}</div>`;
        }
        html += '<div class="cal-grid">';
        for (const day of WEEKDAYS) html += `<div class="cal-cell cal-cell--header">${day}</div>`;
        const leading = new Date(month.y, month.m, 1).getDay();
        for (let i = 0; i < leading; i++) html += '<div class="cal-cell cal-cell--empty"></div>';

        const daysInMonth = new Date(month.y, month.m + 1, 0).getDate();
        for (let d = 1; d <= daysInMonth; d++) {
            const ds = isoDay(month.y, month.m, d);
            const dayActs = byDate.get(ds) ?? [];
            const cls = `cal-cell${ds === todayStr ? " cal-cell--today" : ""}${dayActs.length ? " cal-cell--has-events" : ""}`;
            html += `<div class="${cls}" data-date="${ds}"><span class="cal-day-num">${d}</span>`;
            if (dayActs.length) {
                html += '<div class="cal-events">';
                for (const a of dayActs) {
                    const title = escHtml(a.title || "Activity");
                    const eventCls = `cal-event${a.status === "proposed" ? " cal-event--proposed" : ""}`;
                    html += `<div class="${eventCls}" title="${title}" draggable="true" data-act-id="${escHtml(a.id)}" data-move-url="${escHtml(a.moveUrl)}">`;
                    if (a.index) html += `<span class="cal-event-num">${escHtml(a.index)}</span>`;
                    html += `<span class="cal-event-title">${title}</span></div>`;
                }
                html += "</div>";
            }
            html += "</div>";
        }
        html += "</div>";
    });
    return html;
}
