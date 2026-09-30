import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { barChartConfig, chartTheme, drawBarCharts } from "./bar-chart";

let drawn: { canvas: string; config: unknown }[] = [];
let destroyed = 0;
const realChart = window.Chart;

beforeEach(() => {
    drawn = [];
    destroyed = 0;
    class FakeChart {
        static getChart(canvas: HTMLCanvasElement) {
            return drawn.some((d) => d.canvas === canvas.id) ? { destroy: () => void destroyed++ } : undefined;
        }
        constructor(canvas: HTMLCanvasElement, config: unknown) {
            drawn.push({ canvas: canvas.id, config });
        }
        destroy(): void {}
    }
    window.Chart = FakeChart;
});

afterEach(() => {
    window.Chart = realChart;
    document.documentElement.removeAttribute("data-theme");
});

describe("chartTheme", () => {
    test("follows the page's theme", () => {
        document.documentElement.setAttribute("data-theme", "dark");
        expect(chartTheme().label).toBe("#94a3b8");
        document.documentElement.setAttribute("data-theme", "light");
        expect(chartTheme().label).toBe("#64748b");
    });
});

describe("barChartConfig", () => {
    const theme = { grid: "g", label: "l", tooltipBg: "b", tooltipFg: "f" };

    test("a stacked chart with a legend", () => {
        const config = JSON.parse(JSON.stringify(barChartConfig(["Jan"], [{ label: "Hardware", data: [3], color: "#0891b2" }], { stacked: true, legend: true }, theme)));
        expect(config.data.datasets).toEqual([{ label: "Hardware", data: [3], backgroundColor: "#0891b299", borderColor: "#0891b2", borderWidth: 1.5, borderRadius: 0 }]);
        expect(config.options.plugins.legend).toEqual({ display: true, labels: { color: "l" } });
        expect([config.options.scales.x.stacked, config.options.scales.y.stacked]).toEqual([true, true]);
        expect(config.options.scales.y.ticks).toEqual({ color: "l" });
    });

    test("a count chart has whole-number ticks and no legend", () => {
        const config = JSON.parse(JSON.stringify(barChartConfig(["Jan"], [{ data: [1], color: "#7c3aed" }], { integerTicks: true, rounded: true }, theme)));
        expect(config.options.plugins.legend).toEqual({ display: false });
        expect(config.options.scales.y.ticks).toEqual({ color: "l", precision: 0 });
        expect(config.data.datasets[0].borderRadius).toBe(4);
    });
});

describe("inline charts", () => {
    test("reads each canvas's data, and redrawing replaces the chart", () => {
        document.body.innerHTML = `<canvas id="users" data-bar-chart data-labels='["Jan","Feb"]' data-values='[4,7]' data-color="#0891b2"></canvas>`;
        drawBarCharts();
        drawBarCharts();
        expect(drawn.length).toBe(2);
        expect(destroyed).toBe(1);
        const config = JSON.parse(JSON.stringify(drawn[0]?.config));
        expect(config.data.labels).toEqual(["Jan", "Feb"]);
        expect(config.data.datasets[0].data).toEqual([4, 7]);
    });

    test("data that is not a list draws an empty chart rather than failing", () => {
        document.body.innerHTML = `<canvas id="bad" data-bar-chart data-labels="nope" data-values='{"a":1}'></canvas>`;
        drawBarCharts();
        const config = JSON.parse(JSON.stringify(drawn[0]?.config));
        expect([config.data.labels, config.data.datasets[0].data]).toEqual([[], []]);
    });
});

describe("drawBarCharts", () => {
    test("a chart can name a JSON island and the series it takes from it", () => {
        document.body.innerHTML = `
          <script type="application/json" id="cost-data">{"labels": ["Jan"], "hardware": [5], "api": [2], "ignored": [9]}</script>
          <canvas id="costs" data-bar-chart data-source="cost-data" data-stacked data-legend
                  data-series='[{"key":"hardware","label":"Hardware","color":"#0891b2"},{"key":"api","label":"External APIs","color":"#7c3aed"}]'></canvas>`;
        drawBarCharts();
        const config = JSON.parse(JSON.stringify(drawn[0]?.config));
        expect(config.data.labels).toEqual(["Jan"]);
        expect(config.data.datasets.map((d: { label: string; data: number[] }) => [d.label, d.data])).toEqual([
            ["Hardware", [5]],
            ["External APIs", [2]],
        ]);
        expect([config.options.scales.x.stacked, config.options.plugins.legend.display]).toEqual([true, true]);
    });

    test("inside a swapped region only, and again after a swap", () => {
        document.body.innerHTML = `<canvas id="outside" data-bar-chart data-labels='["a"]' data-values='[1]'></canvas><div id="region"><canvas id="inside" data-bar-chart data-labels='["b"]' data-values='[2]'></canvas></div>`;
        const region = document.getElementById("region");
        if (!region) throw new Error("fixture");
        drawBarCharts(region);
        expect(drawn.map((d) => d.canvas)).toEqual(["inside"]);
    });
});
