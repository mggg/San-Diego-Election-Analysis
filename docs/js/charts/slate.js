/*
 * By-slate panels: one panel per candidate slate, each with its own reference
 * line at that slate's share of citywide VAP.
 *
 * The browser counterpart of _plot_slate_panel. Unlike the focal-group charts,
 * the panels here do NOT share a count axis: a slate holding 44% of VAP and one
 * holding 9% produce distributions on very different scales, and forcing them
 * onto one axis flattens the smaller slate into the baseline. The reference
 * lines differ per panel for the same reason.
 *
 * The system comes from the run's shared selection, the same one the focal-group
 * figures use, so switching system switches both tabs together. The panels are
 * keyed by slate and the bars within them by (model, seat count), so a change of
 * selection animates each bar to its new count -- including the per-panel count
 * axis, which moves under it.
 */

import {
  PANEL, seatScale, seatTicks, drawSeatAxis, drawCountAxis, drawGridlines, drawReferenceLine,
  drawLegend, drawPanelTitle, makeTooltip, emptyState, clearEmptyState,
  ensure, ensureSvg, motion, barGeometry,
} from './axes.js';

export function renderSlate(container, bundle, manifest, view) {
  const { runMeta, slateSeats } = bundle;
  if (!slateSeats.length) {
    emptyState(container, 'This run has no per-slate results.');
    return;
  }

  const models = view.models.filter((m) => !m.pooled);
  if (!models.length) {
    emptyState(container, 'Select a voter model other than the pooled combined row.');
    return;
  }
  clearEmptyState(container);

  const system = view.systems[0];
  const slates = runMeta.slates;
  const modelIds = new Set(models.map((m) => m.id));
  const palette = manifest.palette;
  const tooltip = makeTooltip();

  const cols = Math.min(2, slates.length);
  const rows = Math.ceil(slates.length / cols);

  const { width, margin } = PANEL;
  const height = PANEL.height + 6;
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  // The whole body, as in the focal figures: the panel per slate is read
  // against the seats there were to win, not against the largest any slate
  // happened to take. Falls back to the table's own max if the view has no
  // seat count to offer.
  const seatMax = Math.max(view.seatMax || 0, d3.max(slateSeats, (r) => r.seats) || 0);
  const x = seatScale(seatMax, innerWidth);
  const seats = seatTicks(seatMax);

  const geom = barGeometry(x, models.length);
  const order = new Map(models.map((m, i) => [m.id, i]));
  const barX = (d) => x(d.seats) + geom.offset(order.get(d.mode)) - geom.width / 2;

  const svg = ensureSvg(container, 'slates', cols * width, rows * height,
    `Seats by slate under ${system.label}`);

  svg.selectAll('g.panel')
    .data(slates, (d) => d.id)
    .join((enter) => enter.append('g').attr('class', 'panel'))
    .attr('transform', (d, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      return `translate(${col * width + margin.left},${row * height + margin.top})`;
    })
    .each(function (slate, index) {
      const g = d3.select(this);
      const records = slateSeats.filter(
        (r) => r.system === system.id && r.slate === slate.id && modelIds.has(r.mode),
      );
      // Per-panel count axis: see the note at the top of this file.
      const y = d3.scaleLinear()
        .domain([0, (d3.max(records, (r) => r.plans) || 1) * 1.1])
        .range([innerHeight, 0]).nice();

      drawGridlines(g, y, innerWidth);
      drawPanelTitle(g, slate.label, innerWidth);
      drawSeatAxis(g, x, seats, innerHeight, index >= slates.length - cols ? 'Seats won' : null);
      drawCountAxis(g, y);

      ensure(g, 'g', 'bars').selectAll('rect.bar')
        .data(records, (d) => `${d.mode}-${d.seats}`)
        .join(
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
          (exit) => exit.call((sel) => motion(sel)
            .attr('y', innerHeight)
            .attr('height', 0)
            .remove()),
        )
        .on('mousemove', (event, d) => tooltip.show(
          event,
          `<strong>${d.slateLabel}</strong> under ${d.systemLabel}<br>${d.modeLabel}<br>` +
          `${d.seats} seat${d.seats === 1 ? '' : 's'} in ${d3.format(',')(d.plans)} plans`,
        ))
        .on('mouseleave', () => tooltip.hide());

      // Reference line is a share of this system's own seat count, not the
      // run's combined total -- see the note in report.js where `systems` is
      // built with each system's contest winners attached.
      const share = slate.vapShare * (system.winners ?? runMeta.totalSeats);
      drawReferenceLine(g, x, share, innerHeight, tooltip,
        `${slate.label} share of VAP: ${d3.format('.1%')(slate.vapShare)} (${d3.format('.1f')(share)} seats)`);
    });

  drawLegend(container, 'Each panel: that slate’s share of citywide VAP');
}
