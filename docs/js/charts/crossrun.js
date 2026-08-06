/*
 * Cross-run comparison: every run stacked on one shared seat axis, one panel per
 * run-and-system.
 *
 * The browser counterpart of plot_combined_bubbles_all_runs. It stays a single
 * column on purpose -- the whole point is that the seat axes line up vertically,
 * which two columns would break.
 */

import {
  seatScale, drawSeatAxis, drawReferenceLine, makeTooltip, emptyState, ensureSvg,
} from './axes.js';

const MAX_AREA = 240;
const MIN_AREA = 12;

export function renderCrossRun(container, series, manifest) {
  if (!series || !series.length) {
    emptyState(container, 'No completed runs to compare yet.');
    return;
  }

  const palette = manifest.palette;
  const tooltip = makeTooltip();
  const modes = Array.from(new Set(series.flatMap((s) => s.records.map((r) => r.mode))));
  // Pooled row last, matching the per-run charts.
  modes.sort((a, b) => (a === 'combined') - (b === 'combined'));
  const modeLabel = (id) => manifest.modeLabels[id] || id;

  const width = 720;
  const rowHeight = 34;
  const panelHeight = 30 + modes.length * rowHeight;
  // Room above each panel for its title, and one axis band at the foot of the
  // whole stack -- the seat axis is shared, not repeated per panel.
  const margin = { top: 28, right: 16, bottom: 8, left: 96 };
  const axisBand = 44;
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = panelHeight - margin.top - margin.bottom;

  const seatMax = d3.max(series, (s) => d3.max(s.records, (r) => r.seats));
  const x = seatScale(seatMax, innerWidth);
  const seats = d3.range(0, seatMax + 1);
  const y = d3.scalePoint().domain(modes).range([0, innerHeight]).padding(0.6);

  const perModelMax = d3.max(series, (s) => d3.max(s.records.filter((r) => !r.pooled), (r) => r.plans)) || 1;
  const area = d3.scaleLinear().domain([0, perModelMax]).range([MIN_AREA, MAX_AREA]).clamp(true);
  const radius = (plans) => Math.sqrt(area(plans) / Math.PI);

  const totalHeight = series.length * panelHeight + axisBand;
  const svg = ensureSvg(container, 'crossrun', width, totalHeight,
    'Seat outcomes across every completed run');

  // Seat gridlines run the full height of the stack: the point of this chart is
  // that a bubble in one run sits directly above the same seat count in another.
  svg.append('g').attr('transform', `translate(${margin.left},0)`)
    .selectAll('line').data(seats).join('line')
    .attr('class', 'gridline')
    .attr('x1', (d) => x(d)).attr('x2', (d) => x(d))
    .attr('y1', margin.top - 12)
    .attr('y2', series.length * panelHeight);

  // The reference line is the same share of VAP in every run, so it is read off
  // whichever run supplies it rather than recomputed per panel.
  const runByslug = new Map(manifest.runs.map((r) => [r.slug, r]));

  svg.selectAll('g.panel').data(series).join('g')
    .attr('class', 'panel')
    .attr('transform', (d, i) => `translate(${margin.left},${i * panelHeight + margin.top})`)
    .each(function (panel) {
      const g = d3.select(this);

      g.append('text')
        .attr('class', 'panel-title')
        .attr('x', 0).attr('y', -8)
        .attr('text-anchor', 'start')
        .style('font-weight', '600')
        .text(panel.panelLabel);

      g.append('g').attr('class', 'axis')
        .call(d3.axisLeft(y).tickFormat(modeLabel).tickSize(0));

      g.append('g').selectAll('circle')
        .data(panel.records.filter((r) => r.plans > 0))
        .join('circle')
        .attr('class', 'bubble')
        .attr('cx', (d) => x(d.seats))
        .attr('cy', (d) => y(d.mode))
        .attr('r', (d) => radius(d.plans))
        .attr('fill', (d) => palette[d.mode] || '#898781')
        .attr('fill-opacity', 0.75)
        .on('mousemove', (event, d) => tooltip.show(
          event,
          `<strong>${panel.panelLabel}</strong><br>${d.modeLabel}` +
          `${d.pooled ? ' (averaged across models)' : ''}<br>` +
          `${d.seats} seat${d.seats === 1 ? '' : 's'} in ${d3.format(',.1f')(d.plans)} plans`,
        ))
        .on('mouseleave', () => tooltip.hide());

      const run = runByslug.get(panel.runSlug);
      if (run && run.proportionalSeats != null) {
        drawReferenceLine(g, x, run.proportionalSeats, innerHeight, tooltip, run.proportionalLabel);
      }
    });

  // The one seat axis, under the whole stack.
  drawSeatAxis(
    svg.append('g').attr('transform', `translate(${margin.left},${series.length * panelHeight})`),
    x, seats, 0, 'Seats won',
  );
}
