/*
 * Coalition boxplot: each plan's districts ranked by a slate's share of district
 * VAP, low to high, pooled across plans.
 *
 * The browser counterpart of the notebook figure this project and the Chicago one
 * both use. District ids are not comparable between plans -- district 5 in plan 0
 * is not the same geography as district 5 in plan 200 -- but rank position is, so
 * rank is what the boxes are built on. Each box is one rank's distribution of
 * that slate's VAP share.
 *
 * The boxes are the same under both readings -- every district at that rank --
 * so they move only when the group does. Only the fill changes:
 *
 *   Win rate      the share of districts at that rank the group won, counted
 *                 over the ones where it actually fielded a candidate. That
 *                 restriction is load-bearing: without it a rank the group never
 *                 contested would read as a rank it always lost. It applies to
 *                 the number, not to the boxes -- the notebook restricts the
 *                 boxes too, which makes the two readings sit on different
 *                 distributions and the figure jump when you switch between
 *                 them. The contested count is in the tooltip so the denominator
 *                 is never hidden.
 *   Availability  the average number of that group's candidates on the ballot.
 *
 * A rank with no signal at all is filled solid black, so "never on the ballot"
 * reads as categorically different from "rarely won".
 *
 * Both the box statistics and the fill values are computed in
 * summarize_results._availability_artifact, so a box here and its matplotlib
 * counterpart cannot disagree about where a whisker ends.
 */

import {
  drawCountAxis, drawGridlines, makeTooltip, emptyState, clearEmptyState,
  ensure, ensureSvg, motion,
} from './axes.js';

const BASE_COLOR = '#1a7a3c';
const NO_SIGNAL_COLOR = '#000000';
const PANEL = { width: 640, height: 300, margin: { top: 16, right: 16, bottom: 52, left: 54 } };

export function renderAvailability(container, bundle, manifest, view) {
  const { runMeta, availability } = bundle;
  const metric = view.metric || 'winRate';
  const slateId = view.slate.id;
  const system = view.systems[0];

  if (!availability) {
    emptyState(container, 'This run has no candidate-availability data. It is built from the '
      + 'settings files, so a run whose settings have been cleaned up cannot produce it.');
    return;
  }

  // Always the unrestricted boxes: the distribution of VAP share by rank is a
  // property of the districting, not of the election, so it has no business
  // changing when the fill does.
  const box = availability.boxes.find((b) => b.slate === slateId && !b.restricted);
  // The contested subset is still read, for its counts: they are the denominator
  // the win rate is over.
  const contested = availability.boxes.find((b) => b.slate === slateId && b.restricted);
  const contestedAt = new Map((contested?.ranks || []).map((r) => [r.rank, r.n]));
  const color = availability.colors.find((c) => c.slate === slateId && c.metric === metric
    && (metric !== 'winRate' || c.system === system.id));

  if (!box || !box.ranks.length) {
    emptyState(container, `No ${view.slate.label} districts to rank.`);
    return;
  }
  clearEmptyState(container);

  const tooltip = makeTooltip();
  // Whole records, not just the number: they carry the counts the percentage
  // and the average are taken over.
  const values = new Map((color?.values || []).map((v) => [v.rank, v]));
  const valueAt = (rank) => values.get(rank)?.value ?? 0;
  const ranks = box.ranks.map((r) => r.rank);

  const { width, height, margin } = PANEL;
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  const x = d3.scalePoint().domain(ranks).range([0, innerWidth]).padding(0.6);
  const boxWidth = Math.min(38, (innerWidth / Math.max(ranks.length, 1)) * 0.62);

  const lo = d3.min(box.ranks, (r) => d3.min([r.low, ...r.outliers]));
  const hi = d3.max(box.ranks, (r) => d3.max([r.high, ...r.outliers]));
  const pad = (hi - lo) * 0.08 || 1;
  const y = d3.scaleLinear().domain([Math.max(0, lo - pad), hi + pad]).range([innerHeight, 0]).nice();

  // Shaded between the smallest and largest non-zero value, exactly as the
  // matplotlib colorbar is: zero is not on the ramp, it is its own case.
  const positive = [...values.values()].map((v) => v.value).filter((v) => v > 0);
  const scale = d3.scaleLinear()
    .domain([d3.min(positive) ?? 0, d3.max(positive) ?? 1])
    .range(['#ffffff', BASE_COLOR]).clamp(true);
  const fillFor = (rank) => (valueAt(rank) > 0 ? scale(valueAt(rank)) : NO_SIGNAL_COLOR);

  const fmt = (v) => (color?.format === 'percent'
    ? `${d3.format('.1f')(v)}%`
    : d3.format('.2f')(v));
  const pct = (v) => `${d3.format('.1f')(v)}%`;
  const count = (v) => d3.format(',')(v);

  // Before the figure, not after it: the key says what the fill means, which is
  // the first thing a reader needs and the last thing they find underneath.
  drawScaleKey(container, color, positive, scale, fmt);

  const svg = ensureSvg(container, 'availability', width, height,
    `${view.slate.label} share of district VAP by rank, for ${runMeta.runName}`);
  const panel = ensure(svg, 'g', 'panel')
    .attr('transform', `translate(${margin.left},${margin.top})`);

  drawGridlines(panel, y, innerWidth);
  drawCountAxis(panel, y, 5);

  ensure(panel, 'g', 'rank-axis')
    .classed('axis', true)
    .attr('transform', `translate(0,${innerHeight})`)
    .call(d3.axisBottom(x).tickSizeOuter(0));

  ensure(panel, 'text', 'rank-label')
    .classed('axis-label', true)
    .attr('x', innerWidth / 2).attr('y', innerHeight + 38)
    .attr('text-anchor', 'middle')
    .text(`Districts ranked by ${view.slate.label} VAP share (low to high)`);

  ensure(panel, 'text', 'vap-label')
    .classed('axis-label', true)
    .attr('transform', 'rotate(-90)')
    .attr('x', -innerHeight / 2).attr('y', -40)
    .attr('text-anchor', 'middle')
    .text(`${view.slate.label} % of district VAP`);


  ensure(panel, 'g', 'boxes').selectAll('g.box')
    .data(box.ranks, (d) => d.rank)
    .join(
      (enter) => {
        const g = enter.append('g').attr('class', 'box').style('opacity', 0);
        g.append('line').attr('class', 'whisker');
        g.append('line').attr('class', 'cap cap-low');
        g.append('line').attr('class', 'cap cap-high');
        g.append('rect').attr('class', 'box-body');
        g.append('line').attr('class', 'median');
        return g.call((sel) => motion(sel).style('opacity', 1));
      },
      (update) => update,
      (exit) => exit.call((sel) => motion(sel).style('opacity', 0).remove()),
    )
    .each(function (d) {
      const g = d3.select(this);
      const cx = x(d.rank);
      const noSignal = valueAt(d.rank) <= 0;

      motion(g.select('line.whisker'))
        .attr('x1', cx).attr('x2', cx).attr('y1', y(d.low)).attr('y2', y(d.high));
      motion(g.select('line.cap-low'))
        .attr('x1', cx - boxWidth / 4).attr('x2', cx + boxWidth / 4)
        .attr('y1', y(d.low)).attr('y2', y(d.low));
      motion(g.select('line.cap-high'))
        .attr('x1', cx - boxWidth / 4).attr('x2', cx + boxWidth / 4)
        .attr('y1', y(d.high)).attr('y2', y(d.high));
      motion(g.select('rect.box-body'))
        .attr('x', cx - boxWidth / 2).attr('width', boxWidth)
        .attr('y', y(d.q3)).attr('height', Math.max(y(d.q1) - y(d.q3), 1))
        .attr('fill', fillFor(d.rank));
      // A near-black median would vanish against a solid-black box.
      motion(g.select('line.median'))
        .attr('x1', cx - boxWidth / 2).attr('x2', cx + boxWidth / 2)
        .attr('y1', y(d.median)).attr('y2', y(d.median))
        .attr('stroke', noSignal ? '#ffffff' : '#0b0b0b');

      g.selectAll('circle.outlier').data(d.outliers).join('circle')
        .attr('class', 'outlier')
        .attr('cx', cx).attr('r', 2.4)
        .call((sel) => motion(sel).attr('cy', (v) => y(v)));

      /*
       * Every rate as the fraction it actually is.
       *
       * A percentage on its own hides its denominator, and here the denominators
       * differ between the two readings and between ranks: win rate is over
       * district elections -- each contested district counted once per voter
       * model and replicate -- while availability is over districts. Reading
       * "0.13 per district" is much easier as "13 candidates across 100".
       */
      const v = values.get(d.rank);
      const ran = contestedAt.get(d.rank) || 0;
      g.select('rect.box-body')
        .on('mousemove', (event) => {
          let reading;
          if (metric === 'winRate') {
            // Each district is simulated once per voter model and replicate, so
            // the election count is a multiple of the district count. Both, and
            // the multiplier, so the denominator is never a mystery: a rank has
            // 100 districts and 2,000 elections, of which only the contested
            // ones are counted here.
            const per = ran ? Math.round(v.contests / ran) : 0;
            reading = !v || !v.contests
              ? `${view.slate.label} never on the ballot in any of the `
                + `${count(d.n)} districts at this rank`
              : `Won <strong>${count(v.won)} of ${count(v.contests)}</strong> district `
                + `election${v.contests === 1 ? '' : 's'} (${pct(v.value)})<br>`
                + `from the ${count(ran)} of ${count(d.n)} districts where `
                + `${view.slate.label} ran, at ${per} election${per === 1 ? '' : 's'} each`;
          } else {
            reading = !v || !v.candidates
              ? `No ${view.slate.label} candidates in any of the `
                + `${count(d.n)} districts at this rank`
              : `<strong>${count(v.candidates)} ${view.slate.label} candidate`
                + `${v.candidates === 1 ? '' : 's'}</strong> across `
                + `${count(v.districts)} districts (${d3.format('.2f')(v.value)} per district)`;
          }
          tooltip.show(
            event,
            `<strong>Rank ${d.rank}</strong> of ${ranks.length}<br>${reading}<br>` +
            `median ${pct(d.median)} of district VAP, `
            + `middle half ${pct(d.q1)}–${pct(d.q3)}`,
          );
        })
        .on('mouseleave', () => tooltip.hide());
    });

  // The mean across every rank, as the matplotlib figure draws it.
  motion(ensure(panel, 'line', 'mean-line')
    .attr('x1', 0).attr('x2', innerWidth))
    .attr('y1', y(box.overallMean)).attr('y2', y(box.overallMean));

}

/**
 * The colour ramp and the two cases it does not cover, above the figure.
 *
 * The ramp sits on the left, and the two cases it cannot express are boxed
 * together on the right so they read as a legend rather than as stray labels.
 * The ramp is a two-dimensional object -- title, bar, and the values at each end
 * -- and does not belong inside that box.
 *
 * The ramp alone would not say what black means, and black is the reading that
 * matters most: a rank the group was never on the ballot in.
 */
function drawScaleKey(container, color, positive, scale, fmt) {
  const key = ensure(container, 'div', 'box-key');
  key.selectAll('*').remove();
  if (!color) return;

  if (positive.length) {
    const [lo, hi] = [d3.min(positive), d3.max(positive)];
    const ramp = key.append('div').attr('class', 'ramp-block');
    ramp.append('span').attr('class', 'ramp-title').text(color.label);
    // Sampled rather than a two-stop gradient, so the bar shows the same
    // non-linear-looking ramp the boxes are filled from.
    ramp.append('span').attr('class', 'ramp')
      .style('background', `linear-gradient(to right, ${
        d3.range(0, 1.001, 0.1).map((t) => scale(lo + t * (hi - lo))).join(',')})`);
    ramp.append('span').attr('class', 'ramp-min').text(fmt(lo));
    ramp.append('span').attr('class', 'ramp-max').text(fmt(hi));
  }

  const box = key.append('div').attr('class', 'key-box');

  const zero = box.append('div').attr('class', 'key-item');
  zero.append('span').attr('class', 'swatch').style('background', NO_SIGNAL_COLOR);
  zero.append('span').text(color.zeroLabel);

  const mean = box.append('div').attr('class', 'key-item');
  mean.append('span').attr('class', 'swatch line');
  mean.append('span').text('Mean VAP share across all ranks');
}
