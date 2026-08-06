/*
 * Seat-distribution histogram: one panel for the selected voting system, one
 * bar series per voter model.
 *
 * The browser counterpart of _render_method_histograms in summarize_results.py.
 * The pooled "combined" row is left out because these are per-model counts, and
 * the bars are offset and half-overlapped exactly as the matplotlib figure does
 * it -- see barGeometry in axes.js.
 *
 * Bars are keyed by (model, seat count), so changing the system or switching a
 * model off animates the bars that persist to their new heights rather than
 * rebuilding the panel. Which system and models it draws is the run's shared
 * selection (see buildControls in report.js), so this figure and the bubble plot
 * beside it always show the same thing.
 */

import {
  PANEL, seatScale, drawSeatAxis, drawCountAxis, drawGridlines, drawReferenceLine,
  drawLegend, drawPanelTitle, makeTooltip, emptyState, clearEmptyState,
  ensure, ensureSvg, motion, barGeometry,
} from './axes.js';

export function renderHistogram(container, bundle, manifest, view) {
  const { runMeta, focalSeats } = bundle;
  const system = view.systems[0];
  // The pooled row is an average across models, so it has no place among bars
  // that are counts -- it appears in the bubble plot instead.
  const models = view.models.filter((m) => !m.pooled);
  const modelIds = new Set(models.map((m) => m.id));
  const records = focalSeats.filter(
    (r) => !r.pooled && r.system === system.id && modelIds.has(r.mode),
  );

  if (!models.length) {
    emptyState(container, 'Select a voter model other than the pooled combined row.');
    return;
  }
  if (!records.length) {
    emptyState(container, 'No seat data for this selection.');
    return;
  }
  clearEmptyState(container);

  const palette = manifest.palette;
  const tooltip = makeTooltip();
  const { width, height, margin } = PANEL;
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  const maxPlans = d3.max(records, (r) => r.plans);
  const x = seatScale(runMeta.seatMax, innerWidth);
  const y = d3.scaleLinear().domain([0, maxPlans * 1.1]).range([innerHeight, 0]).nice();
  const seats = d3.range(0, runMeta.seatMax + 1);

  const geom = barGeometry(x, models.length);
  const order = new Map(models.map((m, i) => [m.id, i]));
  const barX = (d) => x(d.seats) + geom.offset(order.get(d.mode)) - geom.width / 2;

  const svg = ensureSvg(container, 'histogram', width, height,
    `Seat distributions for ${runMeta.runName}`);
  const panel = ensure(svg, 'g', 'panel')
    .attr('transform', `translate(${margin.left},${margin.top})`);

  drawGridlines(panel, y, innerWidth);
  drawPanelTitle(panel, system.label, innerWidth);
  drawSeatAxis(panel, x, seats, innerHeight, 'Seats won');
  drawCountAxis(panel, y);

  ensure(panel, 'text', 'count-label')
    .classed('axis-label', true)
    .attr('transform', 'rotate(-90)')
    .attr('x', -innerHeight / 2)
    .attr('y', -34)
    .attr('text-anchor', 'middle')
    .text('Plans');

  ensure(panel, 'g', 'bars').selectAll('rect.bar')
    .data(records, (d) => `${d.mode}-${d.seats}`)
    .join(
      // A model or seat count with no counterpart in the previous selection
      // rises out of the baseline; everything else is already on screen.
      (enter) => enter.append('rect')
        .attr('class', 'bar')
        .attr('data-mode', (d) => d.mode)
        .attr('fill', (d) => palette[d.mode] || '#898781')
        .attr('x', barX)
        .attr('width', geom.width)
        .attr('y', innerHeight)
        .attr('height', 0)
        .call((sel) => motion(sel)
          .attr('y', (d) => y(d.plans))
          .attr('height', (d) => innerHeight - y(d.plans))),
      (update) => update
        .call((sel) => motion(sel)
          .attr('x', barX)
          .attr('width', geom.width)
          .attr('y', (d) => y(d.plans))
          .attr('height', (d) => innerHeight - y(d.plans))),
      // Only y and height: an exiting bar's model has left the group, so there
      // is no offset for it to move to.
      (exit) => exit.call((sel) => motion(sel)
        .attr('y', innerHeight)
        .attr('height', 0)
        .remove()),
    )
    // Handlers go on the merged selection, never on the transition it returns.
    .on('mousemove', (event, d) => tooltip.show(
      event,
      `<strong>${d.systemLabel}</strong><br>${d.modeLabel}<br>` +
      `${d.seats} seat${d.seats === 1 ? '' : 's'} in ${d3.format(',')(d.plans)} plans ` +
      `(${d3.format('.1%')(d.share)})`,
    ))
    .on('mouseleave', () => tooltip.hide());

  drawReferenceLine(panel, x, runMeta.proportionalSeats, innerHeight, tooltip,
    runMeta.proportionalLabel);
}
