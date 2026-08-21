/*
 * Shared chart furniture: the seat axis, the proportional-representation line,
 * the legend, and the tooltip.
 *
 * Every chart on the page measures the same quantity on x -- seats won -- so the
 * scale, ticks, and reference line are defined once here rather than per chart.
 * The matplotlib figures do the same thing with _seat_axis_upper and
 * _prop_line_label; these read the values those produced out of run.json rather
 * than recomputing them, so the two renderings cannot disagree.
 *
 * Everything here is idempotent: called twice on the same parent it updates what
 * is there rather than appending a second copy. That is what lets a chart be
 * redrawn on every change of selection while its marks stay put and animate to
 * their new values -- see the note on motion below.
 */

export const PANEL = { width: 300, height: 190, margin: { top: 26, right: 14, bottom: 38, left: 46 } };

/*
 * The margin the two focal figures share.
 *
 * They sit side by side and are read against each other, so their plot areas
 * have to be the same width -- otherwise the same seat count lands at a
 * different x in each and the pair only appears to line up because the tick
 * labels agree. The left is the bubble chart's: it has to clear the voter-model
 * row labels, and the histogram can afford the extra whitespace.
 */
export const FOCAL_MARGIN = { top: 26, right: 14, bottom: 38, left: 78 };

/*
 * Seat-axis tick spacing, coarse enough that labels never crowd.
 *
 * Follows summarize_results._tick_step, so a web chart and its PNG counterpart
 * label the same seats. A step of 1 is right on a small axis -- a 3- or 9-seat
 * body reads straight off it -- and stops being right as the axis grows:
 * sixteen labels across a 300px panel start touching. A wider axis gets a
 * wider step, so the axis still spans every seat while labelling fewer.
 *
 * Where the legible step doesn't divide the body, the next one up that does is
 * preferred: seatTicks always labels the last seat, so a 15-seat axis stepping
 * by 2 runs 0, 2 ... 12, 15 and ends on a gap half again as wide as the rest,
 * which reads as a mistake in the drawing rather than as the end of the axis.
 * Stepping by 3 gives 0, 3, 6, 9, 12, 15 -- evenly spaced, and still only six
 * labels. Capped at twice the legible step so the search can't thin the axis
 * to a label or two, and left alone for a body no larger step divides (a
 * 11-seat one, say), where the uneven last gap is unavoidable.
 */
export function seatTickStep(seatMax) {
  const legible = (() => {
    if (seatMax <= 10) return 1;
    if (seatMax <= 20) return 2;
    if (seatMax <= 50) return 5;
    return 10;
  })();
  if (seatMax % legible === 0) return legible;
  for (let step = legible + 1; step <= legible * 2; step += 1) {
    if (seatMax % step === 0) return step;
  }
  return legible;
}

/**
 * Tick values across 0..seatMax at the step that keeps labels legible, always
 * including the last seat.
 *
 * A step that does not divide the body would otherwise stop short -- fifteen
 * seats at a step of two labels up to fourteen -- and leave the axis looking
 * like it ends one seat before it does, which is the thing the full-width axis
 * is meant to show. The final regular tick is replaced rather than joined, so
 * the end never carries two labels a single seat apart.
 */
export function seatTicks(seatMax) {
  const step = seatTickStep(seatMax);
  const ticks = d3.range(0, seatMax + 1, step);
  if (ticks.length && ticks[ticks.length - 1] !== seatMax) {
    ticks[ticks.length - 1] = seatMax;
  }
  return ticks;
}

/** Integer seat scale spanning 0..seatMax, padded by half a step at each end. */
export function seatScale(seatMax, innerWidth) {
  return d3.scaleLinear().domain([-0.6, seatMax + 0.6]).range([0, innerWidth]);
}

/*
 * Share-of-seats scale, 0..100%, for comparing systems that fill different
 * numbers of seats.
 *
 * Counting seats only compares like with like: three of nine and three of
 * fifteen are the same bubble on a seat axis and very different outcomes, and
 * proportionality lands somewhere different for each. As a share, the
 * proportional line is the slate's share of VAP whatever the seat count, so one
 * dotted line reads across every row.
 *
 * Padded by four points at each end so a bubble at 0 or 100 is not sliced in
 * half by the edge of the plot.
 */
export function shareScale(innerWidth) {
  return d3.scaleLinear().domain([-4, 104]).range([0, innerWidth]);
}

/** Percentage tick values at `step` intervals across 0..100. */
export function shareTicks(step = 20) {
  return d3.range(0, 100 + step, step).filter((v) => v <= 100);
}

/*
 * Bar geometry, translated from _plot_method_histogram in summarize_results.py
 * so a bar here lands where its matplotlib counterpart does:
 *
 *     width  = 2 * groupSpan(n_modes) / (n_modes + 1)   # of one seat step
 *     step   = width / 2                                # centres half a bar apart
 *     offset = (i - (n_modes - 1) / 2) * step           # group centred on the seat
 *
 * The series overlap each other by half a bar rather than being dodged clear or
 * stacked on one centre: enough offset that every bar keeps an exposed edge and
 * its own baseline, enough overlap that the distributions read as one
 * comparison.
 *
 * The width follows from how many series a group holds, so a fixed span would
 * either crowd a three-model group or leave a one-model group looking thin
 * against the same gap: three series at a span tuned for two would sit
 * visibly denser than the two-series groups beside them on the same axis, and
 * a fourth -- which is what adding the Cambridge model makes routine -- would
 * run the group into its neighbour's if the span didn't shrink to make room.
 * groupSpan narrows the total span as the group grows past two series and
 * widens it slightly as it shrinks to one, so the two-series case (its
 * original tuning) is unchanged and every other count reads at roughly the
 * same visual density.
 */
const GROUP_SPAN = 0.84;

function groupSpan(count) {
  if (count <= 2) return GROUP_SPAN + (2 - count) * 0.06;
  return GROUP_SPAN - (count - 2) * 0.12;
}

export function barGeometry(scale, count) {
  // Solving span = (count - 1) * (width / 2) + width for a fixed span.
  const width = (scale(1) - scale(0)) * (2 * groupSpan(count)) / (count + 1);
  const step = width / 2;
  return { width, offset: (index) => (index - (count - 1) / 2) * step };
}

/*
 * Motion.
 *
 * The charts are redrawn, not rebuilt: marks are keyed by what they represent
 * (a voter model at a seat count), so changing the voting system or switching a
 * model off is an update to data that is already on screen. A bar grows or
 * shrinks to its new count, a bubble swells or contracts, the count axis and its
 * gridlines slide to the new scale, and only marks that genuinely have no
 * counterpart in the new selection enter from -- or leave towards -- the
 * baseline. Redrawing from scratch would throw that away and make every change
 * look the same as every other.
 *
 * Anyone who has asked their system for less motion gets none: the values are
 * applied directly to the selection instead of through a transition.
 */
export const MOTION = { duration: 520 };

export function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
}

/**
 * Animate `selection`, or return it untouched when motion is not wanted.
 *
 * Callers chain the same `.attr` calls onto whichever comes back, so a chart
 * states its target values once and never branches on motion itself.
 */
export function motion(selection) {
  if (reducedMotion()) return selection;
  return selection.transition().duration(MOTION.duration).ease(d3.easeCubicOut);
}

/** Select `selector` under `parent`, creating it the first time through. */
export function ensure(parent, tag, className) {
  const existing = parent.select(`${tag}.${className}`);
  if (!existing.empty()) return existing;
  return parent.append(tag).attr('class', className);
}

/**
 * The chart's <svg>, created once and thereafter resized in place.
 *
 * Keeping the element means the marks inside it survive a redraw, which is the
 * whole basis of the transitions above.
 */
export function ensureSvg(container, className, viewWidth, viewHeight, label) {
  const svg = ensure(container, 'svg', className)
    .attr('viewBox', `0 0 ${viewWidth} ${viewHeight}`)
    .attr('role', 'img')
    .attr('aria-label', label);
  svg.style('max-width', `${viewWidth * MAX_SCALE}px`).style('margin', '0 auto');
  return svg;
}

export function drawSeatAxis(g, scale, ticks, innerHeight, label, tickFormat = d3.format('d')) {
  ensure(g, 'g', 'seat-axis')
    .classed('axis', true)
    .attr('transform', `translate(0,${innerHeight})`)
    .call(d3.axisBottom(scale).tickValues(ticks).tickFormat(tickFormat).tickSizeOuter(0));

  if (label) {
    ensure(g, 'text', 'axis-label')
      .attr('x', scale.range()[1] / 2)
      .attr('y', innerHeight + 32)
      .attr('text-anchor', 'middle')
      .text(label);
  }
}

/**
 * A count axis that slides to its new scale.
 *
 * The domain changes whenever the selection does -- a different system peaks at
 * a different number of plans -- so this is transitioned along with the bars it
 * measures. d3's axis interpolates its own ticks when handed a transition.
 */
export function drawCountAxis(g, scale, ticks = 4) {
  motion(ensure(g, 'g', 'count-axis').classed('axis', true))
    .call(d3.axisLeft(scale).ticks(ticks).tickSizeOuter(0));
}

/** Horizontal gridlines, keyed by value so a tick that survives merely moves. */
export function drawGridlines(g, scale, innerWidth, ticks = 4) {
  ensure(g, 'g', 'gridlines').selectAll('line')
    .data(scale.ticks(ticks), (d) => d)
    .join(
      (enter) => enter.append('line')
        .attr('class', 'gridline')
        .attr('x1', 0).attr('x2', innerWidth)
        .attr('y1', (d) => scale(d)).attr('y2', (d) => scale(d))
        .style('opacity', 0)
        .call((sel) => motion(sel).style('opacity', 1)),
      (update) => update
        .call((sel) => motion(sel)
          .attr('x1', 0).attr('x2', innerWidth)
          .attr('y1', (d) => scale(d)).attr('y2', (d) => scale(d))),
      (exit) => exit.call((sel) => motion(sel).style('opacity', 0).remove()),
    );
}

/**
 * A vertical line that slides to its new x rather than jumping to it.
 *
 * Created at its final position -- a line entering the chart has nowhere to
 * travel from, and animating one in from x=0 would read as a value that had
 * changed. Only a line already on screen transitions.
 */
function verticalLine(parent, className, x, innerHeight) {
  let line = parent.select(`line.${className}`);
  const entering = line.empty();
  if (entering) line = parent.append('line').attr('class', className);

  (entering ? line : motion(line))
    .attr('x1', x).attr('x2', x)
    .attr('y1', 0).attr('y2', innerHeight);

  return line;
}

/**
 * The dotted proportional-representation line. Its position and wording both
 * come from run.json, so the page never recomputes the share it describes.
 *
 * It moves whenever the selected system's seat count does -- a run whose
 * contests differ in size (1 X 6 STV against 9 X 1 IRV against their combined
 * 15) puts proportionality at a different number of seats for each -- so it is
 * transitioned like the axis and gridlines it is read against. Runs whose
 * systems all share one seat count never move it, which is why a line applied
 * directly looked correct for as long as they were the only runs on the page.
 */
export function drawReferenceLine(g, scale, proportionalSeats, innerHeight, tooltip, label) {
  const x = scale(proportionalSeats);
  const line = verticalLine(g, 'reference-line', x, innerHeight);

  if (tooltip && label) {
    // A transparent line over the dotted one, wide enough to be hoverable. It
    // travels with the line it covers, or the hit area is left behind at the
    // old position for the length of the transition.
    verticalLine(g, 'reference-hit', x, innerHeight)
      .attr('stroke', 'transparent').attr('stroke-width', 8)
      .style('pointer-events', 'stroke')
      .on('mousemove', (event) => tooltip.show(event, label))
      .on('mouseleave', () => tooltip.hide());
  }
  return line;
}

/** One tooltip element per page, moved and filled as charts ask for it. */
export function makeTooltip() {
  let node = d3.select('body').select('.chart-tooltip');
  if (node.empty()) node = d3.select('body').append('div').attr('class', 'chart-tooltip');
  return {
    show(event, html) {
      node.html(html)
        .style('left', `${event.pageX + 12}px`)
        .style('top', `${event.pageY - 10}px`)
        .style('opacity', 1);
    },
    hide() { node.style('opacity', 0); },
  };
}

/**
 * The reference line's key, plus any note the chart wants beside it.
 *
 * Deliberately not a legend of voter models: the control bar above the figures
 * already carries a coloured swatch per model, and repeating them under every
 * chart is the same key printed three times on one screen.
 */
export function drawLegend(container, referenceLabel, note) {
  const legend = ensure(container, 'div', 'chart-legend');
  legend.selectAll('*').remove();

  if (referenceLabel) {
    const key = legend.append('span').attr('class', 'key');
    key.append('span').attr('class', 'swatch line');
    key.append('span').text(referenceLabel);
  }
  if (note) legend.append('span').attr('class', 'key').text(note);
  return legend;
}

/** Panel title, drawn in the space PANEL.margin.top reserves. */
export function drawPanelTitle(g, text, innerWidth) {
  ensure(g, 'text', 'panel-title')
    .attr('x', innerWidth / 2)
    .attr('y', -10)
    .attr('text-anchor', 'middle')
    .text(text);
}

/*
 * Cap a panelled chart's on-page width so every chart draws at the same scale.
 *
 * The SVG scales to its viewBox, so a one-panel chart stretched across the
 * column would render its 10px labels at twice the size of the same label in a
 * six-panel chart. Capping each chart at the same multiple of its natural width
 * keeps type and bar weight consistent between one figure and the next; a wide
 * chart still fills the column, a narrow one centres.
 */
const MAX_SCALE = 1.4;

/**
 * Replace the chart with a message.
 *
 * This clears the container, so the next successful render starts from nothing
 * and builds its marks fresh -- there is no half-torn-down chart to update into.
 */
export function emptyState(container, message) {
  container.selectAll('*').remove();
  container.append('p').attr('class', 'chart-empty').text(message);
}

/** Drop a previous message so a recovering chart is not drawn underneath it. */
export function clearEmptyState(container) {
  container.selectAll('p.chart-empty').remove();
}
