# Berkshire consolidated assets

A deterministic SEC-to-static-HTML dashboard for 100% of Berkshire Hathaway's consolidated assets.
Forked from [jl3032/13f-analysis](https://github.com/jl3032/13f-analysis), retaining its MIT attribution
and adapting its dependency-free HTML design. Agent-generated investment commentary is not part of this application.

## Accounting contract

The denominator is consolidated total assets from the same quarter's 10-Q, or the December 31 10-K.
Never add the 13F portfolio total to the balance sheet or proportionally scale it to the equity-securities balance.

- Cash includes cash equivalents, including Treasury bills classified as cash equivalents. Restricted cash stays in Other.
- T-Bills means the separate short-term investment balance. Unsettled purchases are included; associated liabilities are not netted.
- AAPL, AXP, GOOGL, KO, BAC and CVX are ordinary-stock 13F market values, identified by CUSIP.
- GOOGL is Alphabet Class A only. GOOG Class C remains in Other equities if held.
- OXY means the equity-method carrying value of common stock, not its 13F market value.
- OXY preferred stock and warrants remain in Other equities. Remaining equity-method investments exclude OXY common stock.
- Other equities is the balance-sheet equity-securities amount less the displayed fair-value stocks. It includes non-13F investments.
- BNSF and BHE mean **net property, plant and equipment only**, not full business assets or valuations.
- Loans and receivables include insurance, reinsurance, trade and finance receivables net of allowances.
- Goodwill and intangibles include rail/energy intangibles originally reported within Other assets.
- Other has a disclosed component breakdown. Rounding reconciliation is separately identified, not concealed by chart normalization.

These are GAAP asset carrying amounts, mixing historical costs, fair values and equity-method accounting.
They are not enterprise values, Berkshire's market capitalization, or an estimate of intrinsic value.

## Data validation

`config/berkshire.json` is the single mapping source. It selects exact local concept names, exact dimension sets,
entity CIK, USD units and instant dates. Namespace collisions, contradictory duplicate facts and missing facts stop the build.
No alternative concept, data provider, cached-quarter substitute or manually entered amount is used.

The independent checks reconcile:

1. Insurance and Other balance-sheet rows to their asset subtotal.
2. Railroad, Utilities and Energy rows to their asset subtotal.
3. Both subtotals to consolidated total assets.
4. BNSF and BHE PP&E to the rail/energy PP&E subtotal.
5. 13F information-table values and entry count to each filing's cover page.
6. Every chart slice and Other component to the final total.

Rounding tolerance follows the XBRL decimals metadata. Amounts stay in whole USD; percentages are computed only for display.
13F units must be unambiguous. Strict XML validation prevents parser recovery or missing-number defaults from becoming asset amounts.
Options and principal-amount securities are not ordinary-stock slices. Multiple manager rows are aggregated by CUSIP.
A restatement replaces the prior 13F report; a new-holdings amendment appends. Ambiguous or replayed amendments stop ingestion.

A missing CUSIP in a verified complete 13F means no disclosed position, not proven zero ownership. Confidential holdings
can remain within Other equities. Related managers' separate 13Fs are not automatically added.
A selected stock changing to equity-method accounting requires a reviewed classification update.

## Scope and current validation status

The initial history starts at 2025-Q4. Change `since` in the config to expand history after validating those filings.
The accounting policy is bounded to 2023-Q1 onward, when OXY common stock was already an equity-method investment.

The test fixture uses official 2026-Q2 balance-sheet amounts, but its XBRL context shapes and 13F positions are
**synthetic**. It is never a production dataset. Passing offline tests does not establish successful live SEC extraction.

A real SEC contact and a successful live build are required before publication. In particular, the exact current-filing
concept/dimension selectors for the rail/energy intangible-assets disclosure require live verification.
If a filing changes its taxonomy, inspect it and review the mapping; do not insert a guessed amount or broaden the selector.

## Run and test

Python 3.14 and the pinned dependencies in `requirements.txt` are used by CI.
On the managed Windows PC, provision dependencies only with the envx shared runtime manager.
Do not create a repository-local `.venv` or run a repository-local package install.
GitHub-hosted jobs install packages into the runner's interpreter, outside the checkout.

```powershell
envx-python -B tests/run.py
envx-python -B tests/check_sdk.py
```

The first command checks the accounting model, failure behavior and UI with synthetic inputs.
It sends unittest reports to stdout and preserves failure exit codes, so strict PowerShell harnesses do not mistake successful test reports for native-command errors.
The second checks the actual installed EdgarTools XBRL and 13F decoders offline.
Browser tests require the managed Chromium revision for the pinned Playwright version. No GUI or screenshots are used.

For live ingestion, `SEC_USER_AGENT` must contain a descriptive application name and a real contact mailbox you control.
Placeholders, reserved domains and GitHub no-reply addresses are rejected. Set `EDGAR_RATE_LIMIT_PER_SEC` to 1, 2 or 3.
Manage local environment settings with envx; do not copy a contact from somebody else's repository.

```powershell
envx-python -B scripts/update.py inspect --output C:/dev/tmp/brk-xbrl-inspection
envx-python -B scripts/update.py build --output C:/dev/tmp/brk-site
```

Outputs must be new directories outside the repository. `inspect` exports the latest financial filing's enriched facts
and source metadata to aid selector review. `build` resolves all quarters from SEC filings, then writes a complete site.
No fixture-mode build command exists. Serve the resulting site through a static HTTP server, not `file://`.

## Public GitHub Pages deployment

The workflow is public-repository-only. It verifies on main pushes and pull requests, and publishes on manual or scheduled runs.
There is no generated-data commit, automatic git push, runtime API server, database, Hugging Face dependency or LLM service.

1. Configure Pages to deploy through GitHub Actions.
2. Set the real contact identity using `gh secret set SEC_USER_AGENT --repo expgolemclone/13f-analysis`.
3. Run `gh workflow run update.yml --repo expgolemclone/13f-analysis`.
4. Verify the live source mappings, reconciliation and deployment result before treating the site as operational.

The schedule checks weekly throughout the year and daily on days 10-25 in February, May, August and November.
Original and amended filings are reconsidered on each run. Immutable filings may be cached by EdgarTools;
SEC remains the source of truth, and the site can be rebuilt without an Actions cache.

A newly ended quarter stays pending until both source types are available. Older complete quarters remain selectable,
but are never shown as the pending quarter's allocation. The UI displays the verification timestamp and warns after 14 days.
Build failures never publish partial output. GitHub can disable public schedules after inactivity; monitor workflow failures
and the site's last-success timestamp, and re-enable the workflow when needed.

## Layout

```text
config/berkshire.json      CIK, history boundary, XBRL selectors and CUSIPs
scripts/assets.py         Pure extraction, allocation and reconciliation
scripts/update.py         EdgarTools adapter, history builder and inspection
templates/index.html      Static UI, responsive table and accessible chart
tests/                    Offline fixtures, unit tests, browser and SDK checks
.github/workflows/        Public CI and Pages deployment
```

Generated output contains `index.html`, `build.json`, `data/quarters.json` and one JSON per complete quarter.
Pending quarters have metadata only. The index points to the latest complete quarter; no duplicate latest snapshot is generated.

## Sources and license

- [Berkshire annual and interim reports](https://www.berkshirehathaway.com/reports.html)
- [2026-Q2 official report](https://www.berkshirehathaway.com/qtrly/2ndqtr26.pdf), balance sheet p.2,
  PP&E p.14, intangibles p.15, equity-method investments p.10.
- [SEC submissions](https://data.sec.gov/submissions/CIK0001067983.json)
- [EdgarTools](https://github.com/dgunning/edgartools), MIT, used as a dependency rather than a maintained fork.

MIT. See `LICENSE`, retaining the upstream copyright notice. This is not investment advice.
