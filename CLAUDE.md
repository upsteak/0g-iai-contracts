# CLAUDE.md — 0g-iai-contracts

Guidance for anyone (human or agent) working in this repository. `README.md` explains what the
system does; this file explains how to change it without breaking it.

Two things live in their own files because they are long, and this file carries the trigger for
each rather than the content. **Open them when the trigger fires, not only when you are curious:**

- `docs/accepted-risks.md` — R1 to R12. Open the entry **before** adding any bound, guard, floor,
  ceiling or monotonicity check, and before "fixing" anything that matches a line in the index near
  the end of this file. Several of these read as bugs until the entry is read.
- `docs/deployments.md` — the record format and the operator procedures. Open it **before**
  editing anything under `deployments/`, changing a script that reads or writes the record, or
  running a deployment, upgrade, curve swap or handover.

## Build and test

```bash
forge test                                                        # full suite, a few seconds
SIM_LONG=1 SIM_OPS=100000 forge test --match-test test_Sim_Long   # long simulation, minutes
forge build --sizes                                               # contract size check
```

Foundry only. Solidity 0.8.25, cancun, `via_ir`, optimizer 200. OpenZeppelin v5.3.0 for everything —
do not hand-roll access control, pausing, reentrancy guards, safe transfers, or fixed-point maths.

## Engineering rules

These are not style preferences. Each one exists because the alternative has a concrete failure mode.

**1. Never divide twice, and never divide before multiplying.** Every ratio goes through
`Math.mulDiv(a, b, c, rounding)`, whose 512-bit intermediate removes the overflow question entirely.

**2. Every rounding decision favours the protocol.** Value entering the vault rounds **up**; value
leaving rounds **down**. State the direction and the reason in the NatSpec at each site, and cover it
with a test. The aggregate consequence — splitting a mint into many is strictly more expensive than
doing it at once — is asserted in `test/unit/curves/LinearCurveMath.t.sol`, so a reversed rounding fails loudly.

For the step curve the rounding is **one ceiling over the exact bucket sum**, never one per bucket.
The number of buckets a slice touches changes as the supply crosses a boundary, so per-bucket
ceilings would let `cost(s, d)` fall by a wei as `s` rose by one — adjacent prices near the origin
differ by less than 2e18 wei — and the conformance suite's monotonicity test catches exactly that.
With a single ceiling the sum is monotone, `ceil(a) + ceil(b) >= ceil(a + b)` makes splitting never
cheaper, and a non-zero first price makes the result never zero. Keep it that way.

**3. Checks, effects, interactions, and `nonReentrant` on anything that touches an external contract.**
Write all state before any external call. `mint`, `burn`, `harvest`, `stake`, `initiateUnstake`
and `unstake` all carry the guard.

**4. `burn` must never become pausable.** Redemption is a promise to users and the pause
switch must not be able to reach it. `test_Burn_SucceedsWhilePaused` encodes this as an executable
assertion; if you add a shared modifier, check it did not sweep redemption in with it. `mint` no
longer carries `whenNotPaused` but `whenIssuanceOpen` (rule 5), so that check spans both names --
and it has to be anchored to signatures, because the modifier's own definition and two `@dev`
blocks mention them too, so a plain grep returns five lines on a healthy tree and the rule stops
telling signal from noise:

```bash
grep -nE "^\s*function .*(whenNotPaused|whenIssuanceOpen)" src/IAIVault.sol
```

The right answer is exactly two, `mint` and `harvest`. `burn` must never appear.

**5. Roles, not owners.** `AccessControlUpgradeable` with one role per responsibility:
`DEFAULT_ADMIN_ROLE` (grant/revoke, `setFoundation`, `setCurve`, `setHarvestShare`),
`PAUSER_ROLE` (pause/unpause only),
`PAUSE_EXEMPT_MINTER_ROLE` (`mint` while paused, and nothing else), `MINTER_BURNER_ROLE` (held
solely by the vault), and beacon ownership (upgrades). Deployment puts admin, pauser and the beacons
on the deploying account and `PAUSE_EXEMPT_MINTER_ROLE` on nobody, so the paused-mint path opens as
an explicit act of governance.

**There is deliberately no on-behalf redemption, and no role that can perform one.** `burnFor` and
`RESCUE_ROLE` were removed because a rescue is pure expenditure for the caller -- they burn iAI they
bought themselves and the collateral goes to the position owner -- and because it never eliminated
the stranding, only moved it onto whoever sold them those tokens. The stuck case it was for, a
minter whose iAI is unrecoverable, is accepted: documented for integrators, and addressable by an
upgrade that knows the actual case rather than by a standing privilege nobody will use. Do not
reintroduce `burnFor` as a convenience.

`PAUSE_EXEMPT_MINTER_ROLE` is a permission for one operation, not a seat: it is deliberately **not**
part of the handover, so there is nowhere for a stale holder to be written down, and the handover
only checks that the *deployer* is not left holding it. Its cost to `pause()` is R10.

**`DEFAULT_ADMIN_ROLE` on the vault is an upgrade-grade key and must go to the same multisig as
beacon ownership.** It was not always: before the curve moved out of the vault, admin could not touch
pricing at all, and separating it from the upgrade key was a real boundary. `setCurve` and
`setHarvestShare` erase that boundary — between them they can reprice all future issuance, move the
supply ceiling without limit (it is the curve's, so a swap moves it), and redirect every future wei
of collateral yield, which is the same economic power an upgrade has. Treating admin as a lesser key
because it once was is the mistake this paragraph exists to prevent.

`./handover.sh grant` then `./handover.sh renounce` moves them, in two transactions on purpose:
`grant` leaves the deployer's own roles in place so the targets can be confirmed to respond (the
beacons it moves outright -- `Ownable` has one owner), and `renounce`
re-reads governance from the chain and refuses unless they already hold everything. Beacon
ownership is one-step `Ownable` with no acceptance step, so that precondition is its only safety
net. Never collapse the two steps. Note where the unrecoverable moment actually is: a wrong `Admin`
is fixable while the deployer still holds admin, but a wrong `BeaconOwner` is not fixable at all, so
the Safe has to be confirmed to respond **before** `grant`, not only between the steps.

`renounce` stands the deployer down completely by default; `--keep vault-pauser,registry-pauser`
(or any of `iai-admin`, `vault-admin`, `registry-admin`) leaves those behind, and an unrecognised
name is an error rather than a skipped word. The retention that is actually wanted is the pausers --
a pauser cannot grant, reprice or move a beacon, and closing has to be faster than a multisig.
`PAUSE_EXEMPT_MINTER_ROLE` has no spelling in that list and is always given up.

**6. `SafeERC20` for every external token.**

**7. The supply ceiling is the curve's. Never give the vault a cap of its own again, and never
write `require(newCurve.maxSafeSupply() >= supply)` in `setCurve`.** The vault stores no ceiling:
`mint` and every quote read `curve.maxSafeSupply()` (clamped to the vault's hard bound of 2^127) at
the point of use, so the ceiling moves when, and only when, the curve is swapped. A curve whose top
is below the live supply is the supported way to close issuance — burn-only mode — and it is wanted
precisely in an emergency, which is when a guard like that would block it. It reads as a safety
check and is the most natural wrong instinct here, so it is called out by name. Burn-only needs no
mode flag: `mint`'s `supplyAfter > cap` check is simply always true once the ceiling is under the
supply, and nothing else consults it. `test_BurnOnly_ACurveBelowTheLiveSupplyClosesIssuance` and
`test_SetCurve_AcceptsACurveNarrowerThanTheSupply_AndTheCeilingFollows` fail if anyone adds one.

The vault used to carry an adjustable `cap` beside the curve, with `setCap`; it was removed because
it was a second number able to disagree with the first. `setCurve` checks only that the incoming
curve answers `maxSafeSupply()` at all -- a contract that cannot is refused now rather than
discovered on the first mint -- and does not judge its value.

What was given up with it: a curve that starts reverting *after* installation now takes `cap()`,
`remainingCap()`, every quote, `run.sh status` and `run.sh check` down with it, where `setCap(0)`
used to keep them readable. `burn` never touches the curve and `setCurve` never reads the outgoing
one, so redemption and the exit are unaffected; the upgrade rehearsal records the ceiling as
unavailable rather than failing (`test_SnapshotsABrokenCurveAsAnUnavailableCeiling`). An operator
seeing `status` revert should read it as "the curve is broken, swap it", not as a vault failure.

Note what burn-only does **not** stop: `harvest` is gated by `pause`, not by the ceiling, so a
narrower curve closes issuance while the sweep keeps running, and `pause()` in turn leaves a
`PAUSE_EXEMPT_MINTER_ROLE` holder able to mint. Neither is a wind-down on its own; a full stop is
`pause()` plus revoking that role, and R5 has the procedure.

**8. Do not add redundant state.** A field that mirrors something another contract already knows is a
liability, not a safety net — it costs gas on every write and creates a divergence that has to be
handled. The vault reads `iAI.totalSupply()` directly for exactly this reason; a mirrored `supply`
counter with a fail-closed check was removed because the check was also read by `burn`, so any
divergence would have bricked the one path that must always work.

`Position.claimA0G` is not an exception either. It records which part of a position's claim keeps
its own appreciation, and that cannot be derived from anything else -- not from `claim0G`, not from
the balance, not from the curve. The thing rule 8 forbids in this area is the *other* design:
storing the foundation's accrued yield as a `pendingHarvest` counter. That mirrors what
`balance - owed` already says, drifts the moment anyone sends a0G to the vault directly, and turns
the sweep into an accrual whose result depends on how often someone advances it.

`LinearMintCurve.target` is not an exception to this, and neither are `ExponentialMintCurve.base`,
`.exponent` and `.target`. They are `immutable`, so they cannot drift from anything — nothing reads
them to make a decision, and they enforce nothing. They record how the slope, or the table, was
derived, which is the only way the published parameters stay readable on chain -- except
`ExponentialMintCurve`'s `Power`, which the generator takes and the contract has no field for: for a
curve whose `Power` is not 1 the three on-chain fields describe a pure exponential it never followed,
and the deployment record, not the chain, is what describes the table. (`anchorCap` and
`top` are different: each is its curve's `maxSafeSupply()`, and so the vault's ceiling while that
curve is in force. The exponential curve's `top` is a constructor argument that the table must
cover and may exceed by less than a bucket, so a ceiling need not fall on a bucket edge.)

`ExponentialMintCurve`'s table is storage, and that is not redundant state either: it *is* the
curve. It is written once by the constructor and there is no function that writes it again — no
setter, no owner, no proxy — so it is as immutable as an `immutable` field, which Solidity cannot
give an array. The property to preserve is "no write path exists", and it is checkable from the
ABI. Do not add one, however administrative it looks; a different table is a different curve and
goes in by `deployCurve` + `setCurve`, where the history list records it.

**9. The obligation is a function of recorded claims, never of the balance.** `harvest` moves the
balance down to what the vault owes; it does not accrue. Write `owed` in terms of `held` -- for
instance the natural-looking `surplus = share * (held - claim0G/rate)` for "only sweep half" -- and
each call takes a cut of what the last one left, so repeated calls drain a surplus that is only
partly the foundation's. `test_Harvest_StaysIdempotentAcrossAChange` pins it: two calls in a row,
and two more across a change of share, all return zero after the first.

**10. A position is reachable only through `_settled`.** It may be several harvest-share changes
behind, and its stored numbers are then stale. A read that skipped the accessor would price a
redemption against a split no longer in force -- silently, with nothing reverting. The mapping is
documented as off limits for that reason; if you add a function that touches a position, go through
the accessor, and if you add a new accessor make it settle too.

## Storage and upgrades

Every contract uses **ERC-7201 namespaced storage** and sits behind its **own `UpgradeableBeacon`**,
so one upgrade cannot reach the others. Implementations call `_disableInitializers()` in their
constructor; initializer calldata rides in each proxy's constructor so deploy and init are one
transaction (split in two, anyone could initialize the proxy first and own the curve).

**Adding storage:** append to the end of the namespaced struct. Never reorder, never remove, never
change a type.

**`IAIVault`'s struct has been rewritten three times, and the licence is spent.** The curve-and-cap
refactor reordered and retyped every field; the adjustable harvest share reshaped `Position` (a
second claim and an epoch marker, `iaiOutstanding` narrowed to `uint128`) and inserted two fields
and an array before `positions`; removing the vault's own `cap` took the second field out, moving
everything after `curve` up one slot. Each was safe only because mainnet did not exist and Galileo
was rebuilt from scratch rather than upgraded. **Append from here.**

The reason it must: pointing a new implementation at a proxy from *before* a rewrite fails silently
and plausibly rather than loudly. Fields land one or two slots off -- `foundation` reads what was
`cap`, `epochs` lands on a slot that reads zero so `_settled` underflows on `$.epochs.length - 1`,
and `positions` moves so every position reads zero. Every `burn` then reverts, permanently, with the
collateral behind it. Changing the namespace string does not help; the positions still read zero.
**Never point a beacon from before the current deployment at this implementation** -- the most
recent of them is `0xD86E78459687f7f58Da6d1CEA20809BD0c7281a0`, named here so it is recognisable in
an old record.

**Pricing is no longer inside the beacon.** `IAIVault` stores a curve address and calls it; the
maths lives in a separate immutable contract. So an upgrade rehearsal that only compared the vault's
own numbers would miss the one thing an upgrade can still do to reprice the system — repoint
`curve`. `UpgradeChecker` snapshots that address for exactly this reason.

**Before any upgrade touches a live chain, rehearse it on a fork.** This is not optional and it is
not replaced by a unit test:

```bash
./upgrade.sh rehearse vault      # forks the configured chain, upgrades there, compares state
./upgrade.sh vault               # only after the rehearsal passes
```

The rehearsal snapshots the curve address (which carries the ceiling with it), the accounting
totals, live pricing and the positions named in `CHECK_ACCOUNTS`, upgrades, and reverts on any
drift. On a live upgrade, set
`CHECK_ACCOUNTS` to the largest holders.

**The `forge inspect <Contract> storageLayout` diff printed beside it is decoration, and must not
be read as a layout check.** Every contract here keeps its state in an ERC-7201 struct reached by
assembly, so solc reports *zero* state variables and prints an empty table for all three contracts.
The diff therefore says "(identical)" across any layout change at all, including a full rewrite --
the exact silent failure the paragraphs above are about -- and `upgrade.sh` runs both sides against
the same working tree anyway. What does the real work is the state comparison in `UpgradeChecker`;
for layout, diff the namespaced struct itself.

There is deliberately **no on-chain self-check** for upgrade safety. A guard a contract computes
about itself is only sound while it reads the right storage slots, which is precisely what is in
doubt when a layout has shifted — it would report "fine" in the exact case it exists to catch.

## Scripts: chain work and disk work are separate

Every script splits in two, and the split is load-bearing:

- The part that touches a chain is an `internal` function on an abstract contract —
  `IAIDeployer` (system wiring, mock collateral), `AccountFunder` (deriving and funding),
  `UpgradeChecker` (capture, compare, point a beacon), `RoleHandover` (moving governance off the
  deployer). No file access, no environment reads.
- The `*.s.sol` script is a thin shell around it: read the parameters, call the function, write
  the result back.

That is what lets the tests reach the logic without a filesystem. It also keeps the deployment
described in exactly one place — the fixture and the script call the same function, so they
cannot drift.

The abstract halves must stay `internal` functions on inherited contracts, never deployed
helpers. `BeaconProxy` delegatecalls `initialize` during construction, so `msg.sender` there is
whoever ran the `new`; calling out to a separate deployer contract would hand
`DEFAULT_ADMIN_ROLE` to that contract instead of to the deploying account.

## Testing

Three layers, all required to stay green:

- **`test/unit/`** — per-function behaviour, golden vectors for the curve, full revert and
  permission matrices, and the scripts' chain work via the abstract halves above.

  **`test/unit/EpochMath.t.sol` holds the split's arithmetic against a replay.** The library
  reaches the present in one step, by a ratio of running products; the test replays every change
  one at a time, from an implementation that exists only in the test file. They agree to within a
  stated bound -- the replay floors two buckets per step while the shortcut floors once at the end
  -- and exactly, to the wei, when only one change separates the position from the present. Do not
  make the test import the shortcut's algorithm.

  **Be precise about what that buys.** The replay is independent of the *compression*, not of the
  *single step*: it applies the same `V = c + a*r; c' = s*V; a' = (1-s)*V/r`. What holds that step
  honest is elsewhere -- the golden vectors, computed by hand before any of this was written;
  `testFuzz_AChangePreservesValue`, which constrains it without assuming the formula; and
  `testFuzz_SplitKeepsOneMinusShareOfTheDeposit` and
  `testFuzz_SettlementRestoresTheCanonicalCapture`, which pin the economics it exists to deliver.
  The last replaced an assertion that built `claimA0G` from `value * (1 - share) / rate` and checked
  it equalled `backing * (1 - share)` -- an identity of its own arithmetic, which could not fail.
  **A capture test has to be applied to something a function returned, not to something the test
  built.**

  **`test/sim/EpochSim.t.sol` is the seeded long run over that arithmetic.** The property suite
  above is Foundry fuzz: a thousand runs per property on a seed that changes each time, good for
  discovery and useless for reproduction. This one is the repository's usual instrument -- a fixed
  seed consumed in order, 10k steps by default and 100k under `SIM_LONG=1` -- over eight positions
  and a growing history of the split. It reaches the state fuzzing cannot: a position opened at one
  epoch, settled several changes later, replaced, and settled again, for as long as the run lasts.
  Its dominance assertion caught a real modelling error while it was being written -- subtracting a
  position's *stale* claims from totals restated at every change removes too little, and the totals
  stop covering the positions within a few dozen steps. That is rule 10's reason, made executable.

  **`test/unit/HarvestShare.t.sol` holds what the arithmetic cannot state on its own**: that a
  change moves nothing between minter and foundation, that the sweep stays idempotent across one,
  that settling late lands where settling at every step does, that catching up costs the same
  however many changes were missed, and that redemption works from every state a change can leave
  behind -- paused, the ceiling at zero, and several epochs behind at once.

  **`test/unit/curves/CurveConformance.t.sol` is the gate on `IMintCurve`.** It is an abstract
  suite stating the behaviours a signature cannot: `cost` rounds up and is never zero, it is
  monotonic in supply, splitting a mint is never cheaper, a quote is affordable, and the curve is
  evaluable across the domain it declares. It is written entirely in terms of `cost`,
  `quoteForValue` and `maxSafeSupply`, so it applies unchanged to a curve of any shape and a curve
  cannot pass it by reporting figures that agree with each other while disagreeing with what it
  charges. **A new curve is not fit to point the vault at until it inherits this and passes.**
  **Unit tests never touch the filesystem.** Not `vm.readFile`, `vm.writeJson`, `vm.createDir`,
  `vm.projectRoot`, or `vm.setEnv` — the last one because it writes the *process* environment,
  which parallel test contracts share. Check it with:

  ```bash
  grep -rnE "vm\.(readFile|writeFile|readJson|writeJson|createDir|readDir|projectRoot|setEnv)" \
      test/unit test/sim test/Base.t.sol && echo "unit tests must not touch files"
  ```
- **`test/sim/`** — a seeded randomized simulation against a shadow model. The shadow recomputes the
  curve with plain checked arithmetic instead of `Math.mulDiv`, so a rounding regression shows up as
  a disagreement; it evaluates the same algebraic expansion the contract does, and the algebra is
  pinned separately by golden vectors computed outside this codebase. State is compared after *every*
  step so a mismatch names the operation that caused it. Coverage counters are asserted at the end,
  so a run that degenerates into no-ops fails instead of passing vacuously.

  Pausing, curve swaps (which are also how the ceiling moves; two in five draws try to put it
  below the live supply, and about one in five swaps actually does, since a draw needs a supply
  to be below), changes of the harvest share and rejected operations are all part of the
  operation mix. That makes "redemption is never gated" a property held across the whole run rather
  than one assertion, against both switches: a 10k-operation run redeems ~975 times while paused and
  ~390 times with the ceiling below the live supply. It also checks **which** error each guard
  raises from whatever state the run has reached — `_opMint` draws its amount without reference to
  the ceiling and lets the shadow decide whether the mint should be refused, which is a stronger
  statement than a `supply <= cap` assertion and, unlike one, survives burn-only mode.

  The harvest share is retuned throughout, to zero and one as well as between them, and the shadow
  tracks both halves of every claim in plain checked arithmetic rather than through `EpochMath` --
  same formulas, same flooring, different code, so a rounding regression shows up as a
  disagreement. That is all it is, though: the shadow restates the production algorithm, so its
  per-step equality cannot catch a *wrong* formula, only a changed one. The epoch arithmetic is
  held to an independent standard in `EpochMath.t.sol` and `EpochSim.t.sol`, not here; what this
  simulation adds on top of them is the two inequalities -- every position can be paid, and the
  obligation stays within its stated ceiling of the balance -- which are model-independent. Positions are deliberately left unsettled in the shadow exactly as the vault leaves
  them, so the per-step comparison is a check on the lazy settlement itself; a run redeems hundreds
  of positions that have sat through one or more changes.

  Both of those are stated as bounds rather than equalities, on purpose: the totals are restated
  rounded up where a position is rounded down, so they sit a few wei above the sum of the positions,
  and `owed` is deliberately a ceiling that may exceed the balance by a wei without anyone being
  short. Tightening either to an equality is how a rounding rule gets reversed to make a test pass.

  Curve swaps alternate between the two shapes -- a random monotone step table of 500-iAI buckets,
  then a linear curve whose anchor is its ceiling -- and go in both directions, a narrowing swap
  drawing fewer buckets so its top lands under the live supply. A full table is 38 buckets, which
  reaches 19,000 iAI: past any ceiling a linear swap can set, and that is the point of the number.
  Shrink it and the alternating swaps stop covering the case where the incoming curve is wider. The shadow tracks the kind in force
  and prices each mint accordingly, so a mint after a swap is priced at the new curve while a burn
  of a pre-swap position is still settled at that position's own average: the guarantee that a swap
  reprices nothing already minted, checked wei for wei thousands of times from states no
  hand-written test reaches. It never reads the ceiling back -- that is `buckets * width` or the
  anchor by construction, and both are asserted against the deployed curve.

  Adding an operation redraws the entire deterministic sequence, including the sub-sampling inside
  `_opRejection`. Make simulation changes in one pass, then re-run 10k **and** 100k and recalibrate
  the coverage floors against what the new sequence actually produces. Rejected operations deliberately take no state snapshot: the EVM already
  rolls back a reverted frame, and the shadow is not advanced for a rejected operation, so the
  per-step comparison already fails if the contract kept anything.
There is deliberately **no Foundry `invariant_` layer**. It was considered and dropped: the seeded
simulation already runs the same invariants over 100k operations, and an empty `test/invariant/`
directory beside a dead `[profile.default.invariant]` block is worse than neither. If it is ever
added back, add the tests and the config together.

- **`test/script/`** — the only place that touches disk, and only for what genuinely needs it:
  reading the parameter file, writing the addresses back, and the artifact the account script
  produces. Real bugs were found here (`vm.writeJson`'s silent no-op on a missing key), so it
  cannot be dropped — but everything that does not need a file belongs in `test/unit/`.

**The unit fixture must keep building the system through `IAIDeployer`.** `test/Base.t.sol` inherits
the same abstract contract the deploy script does, so every test run rehearses the real deployment
including its post-deploy assertions. Writing the wiring out by hand in the fixture is how a suite
stays green against a topology the script no longer produces — that exact drift once hid a deployment
that granted no `PAUSER_ROLE`, which would have shipped the system permanently paused with nobody able
to open it.

Two Foundry behaviours worth knowing before writing tests here:

- **The filesystem is not rolled back between tests**, only EVM state — and tests within one
  contract run **in parallel** (measured: three tests entered in the same millisecond, 4.8s wall
  against 14.5s CPU). So any test that writes files needs a path of its own, or two of them race
  over the same file and the suite goes flaky rather than failing honestly.
- **An argument is evaluated before the call it belongs to**, so anything that makes an external
  call in an argument position steals the `vm.prank` or `vm.expectRevert` intended for the outer
  call. `vault.grantRole(vault.PAUSER_ROLE(), x)` pranks `PAUSER_ROLE()`; `beacon.upgradeTo(address(
  new Impl()))` pranks the deployment. This has cost time three times in this repo. Hoist role
  constants and freshly deployed addresses into locals first.
- **`vm.warp(block.timestamp + x)` cannot be repeated inside one function.** Under `via_ir` the
  compiler reads `TIMESTAMP` once per function and reuses the value across the cheatcode calls in
  between, so the second and later warps target the *same moment as the first* and the clock
  silently stops advancing -- no revert, no warning, just a test that no longer exercises the
  elapsed time it claims to. Three tests in this suite were doing exactly that and were found only
  when a fourth one's assertion happened to depend on it. Use `_warp(by)` from `test/Base.t.sol`,
  which reads the timestamp back through `vm.getBlockTimestamp()` and so cannot be folded away.
  Check with:

  ```bash
  grep -rn "vm\.warp(block\.timestamp" test && echo "use _warp() instead"
  ```
- **`vm.getRecordedLogs()` drains the buffer.** A second call after the same `vm.recordLogs()`
  returns an empty array, so a helper that fetches internally can only be used once per
  transaction. A test needing two events out of one call must fetch the logs itself and search the
  array — that is what `_onlyIn` is for in `test/unit/Events.t.sol`.
- **`setUp()` runs once**, and every test starts from a snapshot of the state it left. A value
  computed there is therefore identical in every test — including `vm.randomUint()`, which does vary
  when called from a test body but not from `setUp`. That is why the script tests take the directory
  name as an argument (`_bootstrap("some-name")`) instead of generating one: it cannot be derived in
  `setUp`, and a generated name would also change every run, so a failed test could not be inspected
  at a known path.
- **`vm.setEnv` writes the process environment, which parallel test contracts share.** Scripts
  therefore take per-instance overrides (`setDeploymentDir`, `setParams`); `DEPLOYMENT_PATH` and the
  other environment variables remain for the command line.

## Deployment records

**Read `docs/deployments.md` before editing anything under `deployments/`, before changing a script
that reads or writes the record, and before running a deployment, an upgrade, a curve swap or the
handover.** It has the record format, the curve provenance chain, what each curve key answers, and
the procedures themselves.

Two rules are repeated here because breaking either is silent and has already cost a deployment:
**write the whole document, never a single key** -- `vm.writeJson`'s three-argument form does
nothing at all when the key is absent, with no error -- and **never `forge script --resume` a
script that writes its own record**, because the resumed run re-simulates from scratch and records
a fresh set of addresses that were never deployed while the pending transactions land on the old
ones.

## Secrets

- **`deployments/test-accounts-*.json` contains private keys** and is gitignored. `Accounts.s.sol`
  refuses to run on mainnet and refuses to enumerate the deployer's own key — publishing that would
  hand over `DEFAULT_ADMIN_ROLE` and the beacons along with the test accounts. This is a real hazard
  with the stock anvil mnemonic, whose account 0 is the usual local deployer.
- `config.sh` and `.env` are gitignored; commit `config.example.sh` and `.env.example` instead.
- Nothing else in `deployments/` is secret — those files are meant to be committed and shared.
- **Never print a private key, mnemonic, or any other secret into the conversation, into a commit
  message, into a PR body, or into terminal output that gets pasted around.** This applies to keys
  that look disposable: a testnet key is still a key, and one that reaches a chat log or a public
  repository has to be treated as compromised and rotated. When a secret has to be shown to prove
  something, show a derived public value instead — an address, a checksum, or a count. Read `.env`
  and the account files only as far as the task actually requires, and never echo their contents.

## Accepted risks

Decisions, not oversights. The full reasoning for each is in `docs/accepted-risks.md`, and the
index below exists so that a line of it catches your eye at the moment it matters. **If what you
are about to write is a bound, a guard, a rate limit, a monotonicity requirement, or a "surely this
should be checked" anywhere near one of these, open that entry first** -- several of them read as
bugs until you do, and two of the guards people reach for first are forbidden by name in rules 7
and 8 above. Numbering there is append-only, because these are cited from outside the repository.

| | |
| --- | --- |
| R1 | the a0G oracle's write key can drain the vault |
| R2 | a mint and an immediate full burn is free, so round trips cost nothing |
| R3 | a 21-day oracle stall reverts every priced call, redemption included |
| R4 | `totalLocked0G` has no computable upper bound |
| R5 | a falling exchange rate leaves late redeemers short |
| R6 | governance can reprice all future issuance, and lower the curve at holders' profit |
| R7 | the supply ceiling moves with the curve, without limit |
| R8 | a curve can be discriminatory or mutable; the vault cannot tell |
| R9 | `IAI` has no supply cap of its own, so `MINTER_BURNER_ROLE` is unbounded |
| R10 | a pause-exempt minter narrows what `pause()` guarantees |
| R11 | governance can redirect every future wei of collateral yield |
| R12 | a change of the harvest share anchors on the live rate, permanently |
| R13 | an unstaked holder can cycle around an oracle write and keep one step of the sweep |

Three of them carry an **operational requirement** rather than only a consequence: R1 (watch the
oracle; `pause()` and revoke `PAUSE_EXEMPT_MINTER_ROLE` on an unexpected move), R5 (close the
entrance, never the exit, when the vault is short) and R12 (do not move the harvest share while the
oracle is behaving unusually).

## Conventions

- Comments explain *why*, and inline the substance rather than pointing at a document the reader may
  not have. No `see plan §2.A`.
- NatSpec: document every `@param`. The external ABI is documented on the **interfaces**;
  implementations use `@inheritdoc` plus any implementation-specific `@dev`.
- Solidity NatSpec allows only `@custom:*` tags on struct fields — use plain `///` for those.
- Commits and PR bodies carry no AI or tooling attribution, and no personal information.
