## Appendix A. Bounded Ballot Truncation

Section 3.5 describes the truncation applied by default to every scenario's PL and BT profiles: each ballot's ranked length is drawn from one of two empirical Cambridge distributions, chosen by whether the ballot's first choice belongs to the historical majority or minority slate. The **Truncation**

### A.1 Restricting the Historical Distributions


### A.2 Validation

To confirm the bound is respected, we compare each truncated ballot against the length it would have had without truncation. The figure below plots both distributions for the Truncation scenario's 3 × 3 configuration: once under the four-bloc model, where WAIO, HIS, AAPI, and BLK are modeled as four separate blocs, and once under the two-bloc model, where WAIO is set against a single pooled POC bloc.
Under both bloc models, the truncated (actual) ballots fall entirely within the configured `[k, 2k]` window, while the full, untruncated ballots those same voters would otherwise have cast extend out to ten