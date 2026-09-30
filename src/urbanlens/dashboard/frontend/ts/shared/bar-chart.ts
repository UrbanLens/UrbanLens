/**
 * Bar charts in the site's light and dark themes, drawn with the Chart.js global the page loads
 * (``{% vendor_asset "chartjs_js" %}``).
 */

interface ChartInstance {
    destroy(): void;
}

interface ChartConstructor {
    new (canvas: HTMLCanvasElement, config: unknown): ChartInstance;
    getChart(canvas: HTMLCanvasElement): ChartInstance | undefined;
}

declare global {
    interface Window {
        Chart?: ChartConstructor;
    }
}

export interface BarSeries {
    label?: string;
    data: number[];
    /** A ``#rrggbb`` colour; the bar fills with it at 60% opacity. */
    color: string;
}

export interface BarChartOptions {
    stacked?: boolean;
    legend?: boolean;
    /** Whole-number ticks, for counts. */
    integerTicks?: boolean;
    rounded?: boolean;
}

export interface ChartTheme {
    grid: string;
    label: string;
    tooltipBg: string;
    tooltipFg: string;
}

export function chartTheme(root: HTMLElement = document.documentElement): ChartTheme {
    const dark = root.getAttribute("data-theme") === "dark";
    return dark
        ? { grid: "rgba(255,255,255,.08)", label: "#94a3b8", tooltipBg: "#1e293b", tooltipFg: "#f1f5f9" }
        : { grid: "rgba(0,0,0,.06)", label: "#64748b", tooltipBg: "#fff", tooltipFg: "#0f172a" };
}

export function barChartConfig(labels: string[], series: BarSeries[], options: BarChartOptions = {}, theme: ChartTheme = chartTheme()): unknown {
    const stacked = options.stacked ?? false;
    return {
        type: "bar",
        data: {
            labels,
            datasets: series.map((s) => ({ label: s.label, data: s.data, backgroundColor: `${s.color}99`, borderColor: s.color, borderWidth: 1.5, borderRadius: options.rounded ? 4 : 0 })),
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: options.legend ? { display: true, labels: { color: theme.label } } : { display: false },
                tooltip: { backgroundColor: theme.tooltipBg, titleColor: theme.tooltipFg, bodyColor: theme.tooltipFg, borderColor: theme.grid, borderWidth: 1 },
            },
            scales: {
                x: { stacked, ticks: { color: theme.label, maxRotation: 45 }, grid: { color: theme.grid } },
                y: { stacked, beginAtZero: true, ticks: options.integerTicks ? { color: theme.label, precision: 0 } : { color: theme.label }, grid: { color: theme.grid } },
            },
        },
    };
}

/** Draw into *canvas*, replacing any chart already there. */
export function drawBarChart(canvas: HTMLCanvasElement, labels: string[], series: BarSeries[], options: BarChartOptions = {}): void {
    const Chart = window.Chart;
    if (!Chart) return;
    Chart.getChart(canvas)?.destroy();
    new Chart(canvas, barChartConfig(labels, series, options));
}

function parseList<T>(text: string | undefined, isItem: (value: unknown) => value is T): T[] {
    try {
        const value: unknown = JSON.parse(text ?? "");
        return Array.isArray(value) ? value.filter(isItem) : [];
    } catch {
        return [];
    }
}

export const isString = (value: unknown): value is string => typeof value === "string";
export const isNumber = (value: unknown): value is number => typeof value === "number";

function parseJson(text: string | null | undefined): unknown {
    try {
        return JSON.parse(text ?? "");
    } catch {
        return null;
    }
}

function field(value: unknown, key: string): unknown {
    return value && typeof value === "object" ? Reflect.get(value, key) : undefined;
}

function numbers(value: unknown): number[] {
    return Array.isArray(value) ? value.filter(isNumber) : [];
}

/** The series a canvas's ``data-series`` names, each ``{key, label, color}`` taking its data from the island's ``key``. */
function islandSeries(island: unknown, spec: unknown): BarSeries[] {
    if (!Array.isArray(spec)) return [];
    return spec.flatMap((s): BarSeries[] => {
        const key = field(s, "key");
        const color = field(s, "color");
        const label = field(s, "label");
        if (typeof key !== "string" || typeof color !== "string") return [];
        return [{ label: typeof label === "string" ? label : undefined, data: numbers(field(island, key)), color }];
    });
}

/**
 * Draw each ``canvas[data-bar-chart]`` under *root*. A canvas carries one series itself (``data-labels``,
 * ``data-values``, ``data-color``: a count over time), or names a JSON island in ``data-source`` and the series it
 * takes from it in ``data-series``, stacked and with a legend when ``data-stacked`` and ``data-legend`` say so.
 */
export function drawBarCharts(root: ParentNode = document): void {
    for (const canvas of root.querySelectorAll<HTMLCanvasElement>("canvas[data-bar-chart]")) {
        const d = canvas.dataset;
        if (d.source) {
            const island = parseJson(document.getElementById(d.source)?.textContent);
            const labels = field(island, "labels");
            drawBarChart(canvas, Array.isArray(labels) ? labels.filter(isString) : [], islandSeries(island, parseJson(d.series)), { stacked: "stacked" in d, legend: "legend" in d });
        } else {
            drawBarChart(canvas, parseList(d.labels, isString), [{ data: parseList(d.values, isNumber), color: d.color ?? "#0891b2" }], { integerTicks: true, rounded: true });
        }
    }
}

/** Draw every chart now, and those inside each region htmx swaps in. */
export function installBarCharts(): void {
    drawBarCharts();
    document.addEventListener("htmx:afterSwap", (event) => {
        if (event.target instanceof Element) drawBarCharts(event.target);
    });
}
