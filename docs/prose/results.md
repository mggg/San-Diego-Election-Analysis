## 4.1 Basic — 3 × 3 (Baseline)

Against the proportionality of 1.8 of 9 seats (AAPI's 20.4% VAP share), the two score-based rules land closest: pooled across voter models, *Cumulative* averages 1.47 AAPI seats (10.7% of plans give AAPI zero seats, 47.2% give two or more) and *Limited* averages 1.44 (11.2% zero, 44.5% two-or-more). On the other hand, with STV the number of seat is 1.10 seats on average. All voting rules, are just slim proportional for Asian Pacific Island community, but STV lags behind.

The advantages of Cumulative and Limited for minority groups relies on the stacking behaviour. Each voter have a certain number of points that can be distributed across candidates. In this scenario where all have the same cohesion parameter, a voter can put all the available points on their favorite candidate. Allowing blocs with a higher cohesion and lower number of candidates, the advantage to concentrate their votes (bullet ballots). On comparison with STV, this electoral system requires a candidate to surpass a Droop quota, survive elimination and transfer rounds. In this situation, vote splitting really affect minority groups where before surpassing the quota, they can be eliminated.

## 4.4 Nine Seats At-Large

Collapsing the city into one 9-seat at-large district led interesting results. The heavy mass of the Cumulative model is almost equally distributed between 1 and 2 seat, sitting just at the 1.8-seat proportionality line, on average. The Limited model performs very similar, but its heavier mass lies at 1-seat. Likewise, STV results vary on the voter behavior. The Deliberative model concretates more on one seat while the Impulsive voter seat one and two have almost equal probabilities. The combined results lied very similar distributions as the Cumulative and Limited models.

## 4.5 Alternative Electoral Systems — IRV, Plurality, Two-Round Rules

In this sub-section we explore different electoral systems with using the current number of districts. We study four different voting rules--IRV, Plurality, Two-Round and Alaska--with a two round variant for Alaska and Top-Two. As state before, San Diego city elects their representatives on two different rounds, primaries and general elections. On primaries, the first two candidates with the highest number of votes passes to the general elections. On general elections, the candidate with the highest vote wins. One caveat is that on primaries, the electorate can only mark or elect for one candidate.

Based on the previous information, we are going to present the results in two parts. The first analysis will compare the voting rules IRV, Plurality with 3 x 3 STV as they only have one round of elections. The second part of the analysis, we will introduce two-round voting rules, Alaska and Top-Two, and they were elected as they reflect the current state of San Diego elections.

Plurality is by far the best rule here for AAPI voters with 0.80 expected seats. In other words, AAPI win at least one seat 52.1% of the time and with lower probability of reaching 5 seat with an Impulsive voter. On the other hand, IRV performs worst as the majority of the distribution mass lies on zero seats. The reasoning behing these results relies on vote splitting. While on Plurality the minority group benefits from a lower number of candidates so they can concentrate their votes on one single candidate (80% Cohesion), the majority bloc, a.k.a White in this scenario, breaks their power into smaller pieces as they have a higher number of candidates. IRV dynamic benefits the majority bloc as each elimination round, those votes are redistributed to the candidate of their own slate. However, 3 x 3 STV still performing better than Plurality.


The Alaska rule runs a BlockPlurality voting rule on the first round passing top-four candidates and runs a IRV in the second round. The Top-Two runs a Block Plurality and Plurality in the first and second rounds. The Votekit implementation generates only one set of ballots or preference profiles for both voting rules assuming that the electorate don't change from one round to another. However, the turnout rates for each round of elections are different, meaning the electorate is also different. To introduce this variant, we run a second version of these electoral system sampling a completely fresh ballots with a different turnout rates. The results from Alaska and Top-two were very similar. Again meaning 3 x 3 STV performs better.

## 4.6 Low AAPI Turnout


## 4.7 Low AAPI Availability


## 4.8 Diverse AAPI Coalition Preferences

## 4.9 Basic — 3 × 3 + Cambridge Truncation


