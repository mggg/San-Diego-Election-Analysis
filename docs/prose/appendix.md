## A. Ballot Truncation Methodology

Both truncation methods described below act as a second pass over an already-generated PL or BT profile (Section 3.4): they never change which candidates a voter prefers or in what order, only how far down that ranking the ballot is cut off. Both also classify a ballot by its *first-ranked candidate's* slate rather than by the voter's bloc — a minority-bloc voter whose cohesion draw happened to put a majority candidate first is truncated using the majority distribution, matching how the underlying Cambridge data itself was split. A run's config pools every candidate slate into two groups, `majority_slates` and `minority_slates`, and each ballot's truncation length is then drawn independently for every voter who cast it. For instance, a ballot cast by 40 identical voters produces 40 independent length draws, not one length applied to all 40.

### A.1 Cambridge Historical Truncation

This is the default method (`cambridge_truncation.method = "historical"`), used for the Section 3.5 scenarios.

1. **Reduce the historical shapes to this district.** Cambridge's 2009–2017 municipal elections recorded, as Cast Vote Records, the full *shape* of each ballot — not just its length, but which slate occupied each position — separately for ballots that started with the historical majority ("W") slate and ballots that started with the historical minority ("C") slate. VoteKit ships these two shape distributions and a reduction routine (`_reduce_ballot_pmfs`) that rescales a shape distribution to a target candidate count. We call that routine against a stand-in two-slate config whose "majority" and "minority" slates are this district's actual pooled candidate lists.

2. **Collapse to a length marginal.** Truncation only needs to know how many candidates a ballot ranks, so each reduced shape distribution is collapsed to `{length: probability}` by summing the probability of every shape of a given length. This generates two distributions, one for majority-first ballots and one for minority-first ballots, where we are going to sample.

3. **Sample a length per voter.** For each already-generated ballot (PL and BT), its first choice determines whether it draws from the majority or minority distribution. Every voter who cast that exact ranking (the ballot's weight) draws an independent length from that distribution, and the ranking is cut to that length—or left at its full length if the sampled length is longer than the ballot already is. The truncated ballots are then re-grouped into a new profile with the same candidate set.

### A.2 Cap Truncation

This method models a ballot-design rule rather than voter behavior: a jurisdiction that lets each voter rank at most *N* candidates. Unlike A.1, it involves no random draw and does not sort ballots by their first-ranked candidate's slate — every ballot in the affected contest is cut at the same position, and a ballot that is already shorter than *N* is left as it is. It is used for the City Charter Truncation scenario, where the **Ballot length** toggle moves from Full ballots to caps of 10 through 4.

1. The base run (`City Charter Amendment Proposal - Full Ballots`, and its two-bloc counterpart) uses the charter configuration, so every PL and BT voter ranks every candidate in the district. The two-bloc configuration also includes the Cambridge voter model, whose ballots are drawn from Cambridge data and vary in length. In the 1 X 6 STV contest the candidate pool averages 12.18 candidates in the four-bloc run (between 9 and 15) and 11.98 in the two-bloc run (between 10 and 15), so a full PL or BT ballot ranks about twelve candidates.

2. For each cap *N* = 10, 9, …, 4, every profile in the 1 X 6 STV contest keeps only the first *N* ranked candidates of each ballot, with the ballot's weight unchanged, and ballots that become identical once cut are merged into one. The nine 9 X 1 IRV districts are not modified. Every cap is applied to the same ballots — the same districts, candidates and repetitions — so a difference between caps comes from the cap alone and not from a new random draw. This step is implemented in `pipeline/cap_ballot_length.py`.

3. Each cap becomes its own run, named `… - Ballot Cap N`, simulated with the same voting rules and summarized like any other run. Because the 9 X 1 IRV profiles are identical to those in the full-ballot run, their election results are carried over from it and only the 1 X 6 STV elections are run again. The combined 15-seat view therefore changes only through its STV component.

