/*
 * Bubble grid: seats on x, one row per voter model, bubble area showing how
 * often that seat count occurred.
 *
 * The browser counterpart of _render_method_bubbles_figure. Two details carried
 * over deliberately:
 *
 *  - Area, not radius, encodes the count. Radius would exaggerate large values
 *    by squaring them.
 *  - The pooled "Combined" row is an average across voter models, not a sum, so
 *    it sits on the same scale as the rows above it. It is labelled as pooled in
 *    the tooltip rather than being passed off as another model.
 *
 * Bubbles are keyed by (model, seat count): changing the voting system swells or
 * shrinks each one to its new count in place, and switching a model off slides
 * the remaining rows up as that row's bubbles collapse.
 */

import {
  PANEL, seatScale, drawSeatAxis, drawReferenceLine, drawPanelTitle,
  makeTooltip, emptyState, clearEmptyState, drawLegend,
  ensure, ensureSvg, motion,
} from './axes.js';

const MAX_AREA = 260;
const MIN_AREA = 12;

export function renderBubble(container, bundle, manifest, view) {
  const { runMeta, focalSeats } = bundle;
  const system = view.systems[0];
  const models = view.models;                      // pooled row included, last
  const modelIds = new Set(models.map((m) => m.id));
  const records = focalSeats.filter(
    (r) => r.system === system.id && modelIds.has(r.mode) && r.plans > 0,
  );

  if (!records.length) {
    emptyState(container, 'No seat data for this selection.');
    return;
  }
  clearEmptyState(container);

  const palette = manifest.palette;
  const tooltip = makeTooltip();

  const width = PANEL.width;
  // The histogram's panel height, not one derived from the row count: these two
  // figures sit side by side, and a bubble panel that grew and shrank as models
  // were toggled would never line up with the histogram beside it.
  const height = PANEL.height;
  const margin = { top: 26, right: 14, bottom: 38, left: 78 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  /*
   * Area is scaled from this system's per-model counts, whether or not those
   * models are currently on show.
   *
   * Only the per-model counts, because the pooled row averages them and would
   * otherwise shrink every individual bubble. But not only the *selected*
   * records: with the pooled row selected on its own there are no per-model
   * records left to measure, which left the domain at [0, 1] and clamped every
   * bubble to the maximum -- four different plan counts drawn the same size.
   * Reading the basis off the run rather than the selection also means toggling
   * a model no longer resizes the bubbles that stayed.
   */
  const scaleBasis = focalSeats.filter((r) => r.system === system.id && !r.pooled);
  const perModelMax = d3.max(scaleBasis, (r) => r.plans) || 1;
  const area = d3.scaleLinear().domain([0, perModelMax]).range([MIN_AREA, MAX_AREA]).clamp(true);
  const radius = (plans) => Math.sqrt(area(plans) / Math.PI);

  const x = seatScale(runMeta.seatMax, innerWidth);
  const y = d3.scalePoint().domain(models.map((m) => m.id)).range([0, innerHeight]).padding(0.6);
  const seats = d3.range(0, runMeta.seatMax + 1);

  const svg = ensureSvg(container, 'bubbles', width, height,
    `Seat outcome frequencies for ${runMeta.runName}`);
  const panel = ensure(svg, 'g', 'panel')
    .attr('transform', `translate(${margin.left},${margin.top})`);

  drawPanelTitle(panel, system.label, innerWidth);
  drawSeatAxis(panel, x, seats, innerHeight, 'Seats won');

  // The row labels move when a model is switched off, so the axis transitions
  // with the bubbles it names.
  motion(ensure(panel, 'g', 'model-axis').classed('axis', true))
    .call(d3.axisLeft(y).tickFormat((id) => models.find((m) => m.id === id).label).tickSize(0));

  ensure(panel, 'g', 'bubbles').selectAll('circle.bubble')
    .data(records, (d) => `${d.mode}-${d.seats}`)
    .join(
      (enter) => enter.append('circle')
        .attr('class', 'bubble')
        .attr('data-mode', (d) => d.mode)
        .attr('fill', (d) => palette[d.mode] || '#898781')
        .attr('fill-opacity', 0.75)
        .attr('cx', (d) => x(d.seats))
        .attr('cy', (d) => y(d.mode))
        .attr('r', 0)
        .call((sel) => motion(sel).attr('r', (d) => radius(d.plans))),
      (update) => update
        .call((sel) => motion(sel)
          .attr('cx', (d) => x(d.seats))
          .attr('cy', (d) => y(d.mode))
          .attr('r', (d) => radius(d.plans))),
      // An exiting bubble's row has left the scale, so it collapses where it
      // stands rather than moving to a position that no longer exists.
      (exit) => exit.call((sel) => motion(sel).attr('r', 0).remove()),
    )
    .on('mousemove', (event, d) => tooltip.show(
      event,
      `<strong>${d.systemLabel}</strong><br>${d.modeLabel}` +
      `${d.pooled ? ' (averaged across models)' : ''}<br>` +
      `${d.seats} seat${d.seats === 1 ? '' : 's'} in ${d3.format(',.1f')(d.plans)} plans ` +
      `(${d3.format('.1%')(d.share)})`,
    ))
    .on('mouseleave', () => tooltip.hide());

  drawReferenceLine(panel, x, runMeta.proportionalSeats, innerHeight, tooltip,
    runMeta.proportionalLabel);

}
