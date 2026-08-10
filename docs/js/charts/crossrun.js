/*
 * Cross-run comparison: one bubble row per electoral system, on one shared seat
 * axis.
 *
 * The browser counterpart of plot_combined_bubbles_all_runs, but a chart the
 * static figure cannot be: which systems appear is chosen on the page, and any
 * number of them can sit side by side. A row is a (run, system) pair, and rows
 * from different runs are directly comparable because they share the axis.
 *
 * One slate and one voter model at a time, the focal group and the pooled
 * "combined" average by default. Drawing every model at once would triple the
 * height and turn a comparison between systems into a comparison within them,
 * which is what the per-run figures are for; a row here is a voting system and
 * nothing else.
 *
 * The per-slate rows, pooled row included, are built in report_generator, and
 * the focal group's reproduce the focal table exactly -- per model and pooled --
 * so this has one code path rather than a focal case and a slate case that could
 * drift apart.
 *
 * Rows are keyed by series id and bubbles by (series, seat count), so adding or
 * removing a system slides the remaining rows to their new positions and grows
 * or collapses only the bubbles that actually changed.
 */

import {
  seatScale, drawSeatAxis, makeTooltip, emptyState, clearEmptyState,
  ensure, motion,
} from './axes.js';

const MAX_AREA = 300;
const MIN_AREA = 14;
const ROW_HEIGHT = 34;
const WIDTH = 720;
const MARGIN = { top: 14, right: 18, bottom: 42, left: 232 };

export function renderCrossRun(container, manifest, view) {
  const series = view.series;

  if (!series.length) {
    emptyState(container, 'No systems selected yet — add one above to start the comparison.');
    return;
  }
  clearEmptyState(container);

  const runBySlug = new Map(manifest.runs.map((r) => [r.slug, r]));
  const palette = manifest.palette;
  const tooltip = makeTooltip();
  const slateId = view.slate;
  const modeId = view.mode;

  /** The chosen slate as that run sees it -- its own VAP share and wording. */
  const slateOf = (runSlug) =>
    (runBySlug.get(runSlug)?.slates || []).find((s) => s.id === slateId);

  const rowsFor = (s, test) => (s.records || [])
    .filter(test)
    .map((r) => ({ ...r, runSlug: s.runSlug, seriesId: s.id, seriesLabel: s.panelLabel }));

  // One row per selected system, carrying that system's record for the chosen
  // slate and voter model.
  const records = series.flatMap((s) => rowsFor(
    s, (r) => r.slate === slateId && r.mode === modeId && r.plans > 0,
  ));

  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = series.length * ROW_HEIGHT;
  const height = MARGIN.top + innerHeight + MARGIN.bottom;

  // Every seat on the body, not the widest outcome on show: the axis is the
  // thing rows are read against, so it holds still as systems are added and
  // removed, and a row that never elects more than two is visibly short of nine
  // rather than filling the width.
  const seatMax = d3.max(manifest.runs, (r) => r.totalSeats) || 0;
  const x = seatScale(seatMax, innerWidth);
  const seats = d3.range(0, seatMax + 1);
  const y = d3.scalePoint().domain(series.map((s) => s.id)).range([0, innerHeight]).padding(0.5);

  /*
   * Area is scaled from the per-model counts for this slate, whichever model is
   * on show. The pooled row is their average and sits on the same scale, so one
   * basis serves both and switching model no longer resizes bubbles whose counts
   * are comparable. Scaling off the selection alone is what left the per-run
   * bubble chart clamping every pooled bubble to the maximum.
   */
  const scaleBasis = series.flatMap((s) => rowsFor(
    s, (r) => r.slate === slateId && !r.pooled,
  ));
  const maxPlans = d3.max(scaleBasis, (r) => r.plans) || d3.max(records, (r) => r.plans) || 1;
  const area = d3.scaleLinear().domain([0, maxPlans]).range([MIN_AREA, MAX_AREA]).clamp(true);
  const radius = (plans) => Math.sqrt(area(plans) / Math.PI);

  // Not ensureSvg: this one fills its container rather than being capped to a
  // multiple of its natural width like the per-run panels, and its viewBox grows
  // and shrinks with the row count. d3 interpolates the numbers inside a viewBox,
  // so the frame resizes with its contents instead of snapping while they
  // animate.
  const svg = ensure(container, 'svg', 'crossrun')
    .attr('role', 'img')
    .attr('aria-label', 'Seat outcomes across the selected electoral systems');
  const viewBox = `0 0 ${WIDTH} ${height}`;
  if (svg.attr('viewBox')) motion(svg).attr('viewBox', viewBox);
  else svg.attr('viewBox', viewBox);

  const panel = ensure(svg, 'g', 'panel')
    .attr('transform', `translate(${MARGIN.left},${MARGIN.top})`);

  // Seat gridlines run the full height: the point of this chart is that a bubble
  // in one system sits directly above the same seat count in another.
  ensure(panel, 'g', 'gridlines').selectAll('line')
    .data(seats, (d) => d)
    .join(
      (enter) => enter.append('line')
        .attr('class', 'gridline')
        .attr('x1', (d) => x(d)).attr('x2', (d) => x(d))
        .attr('y1', 0).attr('y2', innerHeight)
        .style('opacity', 0)
        .call((sel) => motion(sel).style('opacity', 1)),
      (update) => update.call((sel) => motion(sel)
        .attr('x1', (d) => x(d)).attr('x2', (d) => x(d))
        .attr('y1', 0).attr('y2', innerHeight)),
      (exit) => exit.call((sel) => motion(sel).style('opacity', 0).remove()),
    );

  // Two-line row labels: the run above, the system beneath. One line would need
  // a left margin wide enough for "Alternative Electoral Systems - 9 X 1 Top Two
  // (two-profile)" and leave the plot itself a sliver.
  ensure(panel, 'g', 'row-labels').selectAll('g.row-label')
    .data(series, (d) => d.id)
    .join(
      (enter) => {
        const g = enter.append('g')
          .attr('class', 'row-label')
          .attr('transform', (d) => `translate(-12,${y(d.id)})`)
          .style('opacity', 0);
        g.append('text').attr('class', 'row-run').attr('text-anchor', 'end').attr('dy', '-0.15em');
        g.append('text').attr('class', 'row-system').attr('text-anchor', 'end').attr('dy', '1em');
        return g.call((sel) => motion(sel).style('opacity', 1));
      },
      (update) => update.call((sel) => motion(sel)
        .attr('transform', (d) => `translate(-12,${y(d.id)})`)),
      (exit) => exit.call((sel) => motion(sel).style('opacity', 0).remove()),
    )
    .call((g) => {
      g.select('text.row-run').text((d) => d.run);
      g.select('text.row-system').text((d) => d.systemLabel);
    });

  /*
   * The proportional-representation line, drawn as one segment per row rather
   * than a single full-height rule.
   *
   * Each run has its own share of VAP. When they agree the segments abut and
   * read as one continuous dotted line; when a run with a different share is
   * added, its segment steps aside instead of the whole figure claiming a share
   * that only some of its rows have.
   */
  ensure(panel, 'g', 'reference').selectAll('line')
    .data(series, (d) => d.id)
    .join(
      (enter) => enter.append('line')
        .attr('class', 'reference-line')
        .attr('x1', refX).attr('x2', refX)
        .attr('y1', (d) => y(d.id) - ROW_HEIGHT / 2)
        .attr('y2', (d) => y(d.id) + ROW_HEIGHT / 2)
        .style('opacity', 0)
        .call((sel) => motion(sel).style('opacity', 1)),
      (update) => update.call((sel) => motion(sel)
        .attr('x1', refX).attr('x2', refX)
        .attr('y1', (d) => y(d.id) - ROW_HEIGHT / 2)
        .attr('y2', (d) => y(d.id) + ROW_HEIGHT / 2)),
      (exit) => exit.call((sel) => motion(sel).style('opacity', 0).remove()),
    );

  function refX(d) {
    return x(slateOf(d.runSlug)?.proportionalSeats ?? 0);
  }

  function fill(d) {
    return palette[d.mode] || '#898781';
  }

  // The dotted line is 1.2px wide and effectively impossible to hover. A
  // transparent segment of the same length, ten times as thick, carries the
  // tooltip instead -- the same trick drawReferenceLine uses on the per-run
  // figures, applied per row so each says its own slate's share.
  ensure(panel, 'g', 'reference-hits').selectAll('line')
    .data(series, (d) => d.id)
    .join((enter) => enter.append('line')
      .attr('stroke', 'transparent')
      .attr('stroke-width', 10)
      .style('pointer-events', 'stroke')
      .attr('x1', refX).attr('x2', refX)
      .attr('y1', (d) => y(d.id) - ROW_HEIGHT / 2)
      .attr('y2', (d) => y(d.id) + ROW_HEIGHT / 2))
    .call((sel) => motion(sel)
      .attr('x1', refX).attr('x2', refX)
      .attr('y1', (d) => y(d.id) - ROW_HEIGHT / 2)
      .attr('y2', (d) => y(d.id) + ROW_HEIGHT / 2))
    .on('mousemove', (event, d) => tooltip.show(
      event, slateOf(d.runSlug)?.proportionalLabel ?? '',
    ))
    .on('mouseleave', () => tooltip.hide());

  ensure(panel, 'g', 'bubbles').selectAll('circle.bubble')
    .data(records, (d) => `${d.seriesId}-${d.seats}`)
    .join(
      (enter) => enter.append('circle')
        .attr('class', 'bubble')
        .attr('fill-opacity', 0.75)
        .attr('fill', fill)
        .attr('cx', (d) => x(d.seats))
        .attr('cy', (d) => y(d.seriesId))
        .attr('r', 0)
        .call((sel) => motion(sel).attr('r', (d) => radius(d.plans))),
      // Colour is updated here, not only on enter: a bubble is keyed by (series,
      // seat count), so changing voter model updates bubbles already on screen,
      // and a fill set only at entry left survivors wearing the previous model's
      // colour. It rides the existing transition rather than starting a second
      // one, which would cancel the first and strand the radius mid-flight.
      (update) => update.call((sel) => motion(sel)
        .attr('fill', fill)
        .attr('cx', (d) => x(d.seats))
        .attr('cy', (d) => y(d.seriesId))
        .attr('r', (d) => radius(d.plans))),
      // An exiting bubble's row has left the scale, so it collapses where it
      // stands rather than moving to a position that no longer exists.
      (exit) => exit.call((sel) => motion(sel).attr('r', 0).remove()),
    )
    .attr('data-mode', (d) => d.mode)
    .on('mousemove', (event, d) => {
      const slate = slateOf(d.runSlug);
      tooltip.show(
        event,
        `<strong>${d.seriesLabel}</strong><br>${d.slateLabel} seats · ${d.modeLabel}` +
        `${d.pooled ? ' (averaged across voter models)' : ''}<br>` +
        `${d.seats} seat${d.seats === 1 ? '' : 's'} in ${d3.format(',.1f')(d.plans)} plans` +
        (slate ? `<br>${slate.proportionalLabel}` : ''),
      );
    })
    .on('mouseleave', () => tooltip.hide());

  // The one seat axis, under the whole stack. It slides down as rows are added,
  // in step with the viewBox growing beneath it.
  const axisWrap = ensure(svg, 'g', 'seat-axis-wrap');
  const axisAt = `translate(${MARGIN.left},${MARGIN.top + innerHeight + 8})`;
  if (axisWrap.attr('transform')) motion(axisWrap).attr('transform', axisAt);
  else axisWrap.attr('transform', axisAt);
  drawSeatAxis(axisWrap, x, seats, 0, 'Seats won');
}
