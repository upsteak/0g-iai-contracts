# iAI Contracts

Smart contracts for **iAI**, a compute-entitlement token on 0G. Users lock **a0G** (a yield-bearing
restaking share) as collateral, mint iAI along a rising bonding curve, and stake iAI to earn a daily
allowance of AI compute. Collateral is never sold: redeeming returns the 0G value that was locked
plus the minter's share of what the collateral earned in the meantime, and the rest of that yield is
swept to the foundation. The split is a governance parameter — currently **50/50**.

```
        a0G                    iAI                      compute
  ┌─────────────┐   mint   ┌──────────┐   stake   ┌────────────────┐
  │  collateral │ ───────► │   iAI    │ ────────► │ CreditRegistry │
  │  (locked)   │ ◄─────── │  token   │ ◄──────── │  (entitlement) │
  └─────────────┘   burn   └──────────┘  unstake  └────────────────┘
         │                                             │
         │ harvest (yield only)                        │ off-chain meter
         ▼                                             ▼
     foundation                                  daily allowance
```

## Contracts

| Contract | Role |
| --- | --- |
| `src/IAI.sol` | The ERC-20. Mint and burn restricted to `MINTER_BURNER_ROLE`, held only by the vault; the supply ceiling is the vault's, not the token's. Deliberately **not** `ERC20Burnable` — a holder burning their own tokens would strand the collateral behind them. |
| `src/IAIVault.sol` | Custody, positions, which curve is pricing — and with it the supply ceiling, which the vault reads off the curve rather than storing — and how collateral appreciation is split. `mint` / `burn` / `harvest`. |
| `src/EpochMath.sol` | The bookkeeping behind that split: how a claim is held in two denominations, and why changing the split costs a constant however many times it has changed before. |
| `src/CreditRegistry.sol` | Staking with a cooldown. Records who has how much iAI earning; the allowance itself is metered off-chain. |
| `src/interfaces/IMintCurve.sol` | The pricing surface the vault calls. Three `view` functions, so a curve reaches the vault by `STATICCALL` and can neither write state nor reenter. |
| `src/curves/ExponentialMintCurve.sol` | The curve in force: the exponential curve as a table of bucket prices, 25 iAI per bucket, with the supply ceiling `top` a constructor argument the table covers. The table is storage written once by the constructor and nothing can write it again — no setter, no owner, no proxy — so a curve is still a value, and replacing one means deploying another and repointing the vault. |
| `script/curve/gen_exponential_table.py` | The one definition of how that table is derived from the formula. Standard-library Python; `run.sh check` re-derives and compares. |
| `src/curves/LinearMintCurve.sol` | The original curve, still deployable. Every parameter `immutable`, zero storage. |
| `src/curves/LinearCurveMath.sol` | The linear curve's closed form. A library: no storage, no state. |
| `src/mocks/` | Stand-in a0G, its oracle, and a W0G for the a0G vault to sit over, for networks without the real things. In `src/` rather than `test/` because they are deployed and verified on testnets. `MockA0G` mirrors the real token's shape: an ERC-4626 whose share price comes from the oracle, not from what it holds. |

### The curve

The marginal price rises exponentially in the supply:

```
rate(s) = base · e^(exponent · (s / target)^power)        0G per iAI
```

`power` 1 is the pure exponential; above 1 the price stays near `base` for longer and climbs
harder towards `target`. `power` is a parameter of the table generator and of the deployment
record only: the contract records `base`, `exponent` and `target` as provenance and has no field
for it, so a curve with any other `power` is described completely by its record, not by its
constructor arguments.

There is no `exp` on chain and the vault charges a step function anyway, so the contract holds a
**table**: supply is cut into buckets of 25 iAI, each priced flat at the value the formula takes at
the bucket's **upper** bound, rounded up to the wei — so the table never sits below the smooth
curve. A mint pays the exact sum of price times overlap over every bucket it touches, with one
ceiling at the end rather than one per bucket, which is what keeps the price monotonic at the wei
and makes splitting a mint never cheaper.

The table is produced off chain by `script/curve/gen_exponential_table.py` (standard-library
Python, 60 significant digits), written into the deployment record beside the parameters it came
from, and re-derived and compared entry by entry by `run.sh check`. **The supply ceiling, `top`,
is a parameter of its own** — a constructor argument, not something derived from the table — and
the table is sized to cover it: `ceil(top / bucketWidth)` buckets, the last of which may be only
partly issuable. With the shipped parameters:

| | |
| --- | --- |
| `base` (price at zero supply) | 586 0G / iAI |
| `exponent` | 4.711 |
| `target` (the supply the exponent is normalised against) | 9,270 iAI |
| `top` (the supply ceiling, `maxSafeSupply()`) | **9,270 iAI** |
| `bucketWidth` | 25 iAI, 371 buckets covering 9,275 iAI; the last 5 are priced but not issuable |
| price of the first bucket | 593.49 0G / iAI |
| price at 2,000 iAI (the first public mint after the pre-mint) | 1,639.95 0G / iAI (the smooth curve says 1,619) |
| price at 9,270 iAI | 65,142 0G / iAI on the smooth curve, 111× the base |
| price of the last bucket, [9,250, 9,275) | 65,307 0G / iAI, 110× the first |
| 0G locked by the first 2,000 iAI | 2,046,100 0G |
| 0G locked at the ceiling, 9,270 iAI | 127,838,783 0G (the smooth integral is 127.03M) |
| step between adjacent buckets | 1.28%, everywhere — a pure exponential rises by a constant factor per bucket |

Those are the parameters in `iai-example.json`, which the unit tests pin. **Mainnet (16661) is
priced by a different set**, in `deployments/iai-16661.json`: `base` 1,181.5, `exponent` 5.016,
`power` 3.274, `target` and `top` 9,270 iAI, 25 iAI buckets. On that curve the first bucket is
1,181.50 0G, the bucket after a 2,000 iAI pre-mint is 1,222.93 (the smooth curve says 1,221.25),
the pre-mint itself locks 2,381,968 0G, the last bucket is 179,764.57 (178,178 on the smooth
curve at 9,270 iAI), and the whole table locks 129,259,106 0G against a smooth integral of
127.01M. The step between buckets is no longer constant: 0.2% near the origin, 4.5% at the top,
which is where pricing each bucket at its upper bound costs the most -- about 2% over the smooth
curve across the last 1,270 iAI.

`cost()` is the only pricing primitive. `lockedAt()` floors and exists for charts and reconciliation
only. `priceAt(i)`, `prices()`, `bucketOf(s)` and `rateAt(s)` expose the table for tooling.

**The curve's `top` is the supply ceiling.** The vault has no cap of its own: `mint` and every
quote refuse anything past the curve in force's `maxSafeSupply()`, 9,270 iAI here. `top` is
immutable, so moving it means generating a table that covers the new ceiling, deploying it with the
new `top` and repointing the vault — three commands, and the ceiling moves at the last one. The
constructor refuses a `top` the table does not reach, and a table that runs a whole bucket or more
past it. `base`, `exponent` and `target` are provenance and enforce nothing.

Governance can replace the whole curve, and the ceiling comes with it, so no figure on this page is
a permanent bound — read them from the chain rather than hard-coding them.

### Replacing the curve, and with it the ceiling

Positions record an **absolute amount of 0G**, not the curve parameters that produced it, and
redemption never consults a curve. So swapping the curve reprices nothing already minted: a holder
who minted before a swap redeems for exactly what they locked, and mints after it use the new curve.
A holder who mints on both sides gets one blended average for the whole position — the guarantee is
"nobody's existing collateral is repriced", not "every coin redeems at the price it was minted at".

A curve whose top is below the live supply is a supported state, **burn-only mode**: `mint` refuses,
and redemption, staking and the harvest sweep all carry on untouched. It needs no mode flag
— `mint`'s ceiling check is simply always true once the ceiling is under the supply — and `setCurve`
deliberately does not refuse such a curve. Note that `harvest` is gated by `pause`, not by the
ceiling, so a narrower curve is not a wind-down switch on its own — and `pause()` is not one either
while anybody holds `PAUSE_EXEMPT_MINTER_ROLE`. A full stop is `pause()` plus revoking that role.

One consequence of the ceiling living in the curve: a curve that starts reverting takes `cap()`,
`remainingCap()` and every quote down with it, so `run.sh status` and `run.sh check` fail until it
is swapped out. That is a broken curve, not a broken vault — redemption never consults the curve
and keeps working, and `setCurve` never reads the outgoing curve, so the exit is always open.

### How the yield is split

A position's claim is recorded in **two denominations**, and which one a wei sits in decides who
receives its appreciation:

- **0G-denominated** — redeems for `claim / rate` a0G, so as a0G appreciates it buys fewer shares
  and the difference is left behind for the foundation.
- **a0G-denominated** — returned exactly as deposited, so its appreciation stays with the minter.

There is no third denomination, so the proportion between the two *is* the split, and
`harvestShare` is exactly that proportion (WAD; `5e17` is 50%, `1e18` sends everything to the
foundation, `0` sends everything to minters). A position's marginal capture is fixed when it is
opened and does not drift however far the rate travels, so the split is path-independent: cycling
through a redemption and a fresh mint gains nothing.

`harvest` is unchanged in shape. It moves the vault's balance down to what the vault owes, which is
a function of recorded claims and never of the balance itself — so calling it twice in a row is
harmless, and there is no accrual anyone can advance by poking it.

**Changing the split is prospective in time, not in cohort.** `setHarvestShare` restates every
outstanding position by value at the current rate: appreciation already earned keeps the split it
was earned under, and everything after that point uses the new one. Nothing moves at the moment of
the change — the obligation and every position come out worth what they were worth an instant
earlier. Positions are restated lazily, whenever each is next touched, and catching up costs the
same whether one change was missed or a hundred.

**Value-neutral is not the same as forward-neutral.** A change re-bases the minter's capture onto
the position's current value, which is smaller because the foundation has already taken its part,
and turns the foundation's accrued part into shares that compound for it. So re-issuing the *same*
share still moves a little future yield to the foundation — on the test fixture, about **0.8%** of
a position over two years and twenty-four changes. Admin-only, always in the foundation's
direction, bounded by how often governance acts, and pinned by a test rather than left to be
discovered; accepted risk R11 in `docs/accepted-risks.md` has the measurements and why the
alternative was not taken.

Two consequences worth knowing:

- A change is **refused if the rate has fallen** since the last one. The rate is recorded
  permanently when a change is made and is a divisor in every later settlement, so a reading taken
  during a dip would be baked in. This blocks only the governance action; minting, redemption and
  harvesting are untouched.
- The vault's totals are rounded up where a position is rounded down, so they sit a few wei above
  the sum of the positions they stand for. That residue is claimable by nobody and leaves as
  surplus.

### Rounding

Every division that can lose a wei rounds in the vault's favour: value entering rounds **up**, value
leaving rounds **down**. A consequence worth knowing before reading the tests: splitting one mint
into many smaller ones is strictly *more* expensive than doing it at once, so there is no rounding
arbitrage in either direction.

## What redemption does and does not promise

Burning returns the position's own blend: the **0G value** that was locked, plus the minter's share
of what the collateral appreciated since. Because a0G appreciates, that is still fewer *a0G tokens*
than went in — the 0G value has risen while each token costs more. This is the design, not a loss,
and any UI must denominate in 0G value with a0G counts secondary.

Two caveats are real and must be stated to users:

- **Staked iAI must be unstaked first.** `initiateUnstake` → wait out the cooldown → `unstake` → then
  `burn`. `burn` itself is never pausable, but reaching it can take a day.
- **`totalLocked0G` can exceed what the curve accounts for at the live supply, with no computable
  ceiling.** A redeemer releases 0G at their average rate while the freed supply is resold at the
  marginal rate, so churn ratchets the total upward. There is no numeric bound to quote: the curve
  can be replaced with a longer or dearer one. Never write `require(totalLocked0G <= X)` for
  any curve-derived `X`. It also now rises with the exchange rate, by the minters' share of the
  appreciation.

### What to read, and what to leave alone

Integrate against these, which mean what their names say and are stable:

| Read | For |
| --- | --- |
| `positionOf(account)` | what a position is worth in 0G now, the iAI outstanding, and the 0G behind each iAI. Dividing the first by `exchangeRate()` gives the a0G a full redemption pays |
| `quoteBurn(account, amount)` | the 0G released and the a0G paid for a given redemption |
| `quoteMint(amount)` / `quoteMintForA0G(a0G)` | what a mint costs, and what a given amount of a0G buys |
| `totalLocked0G()` | what every outstanding position is worth, in 0G |
| `harvestShare()` | the foundation's current cut of appreciation, WAD |

`totalClaim0G()`, `totalClaimA0G()`, `positionClaims(account)`, `epochAt(i)` and `currentEpoch()`
are **internal accounting**, exposed so that tests, the deployment checker and monitoring can watch
the two denominations move independently. They are not a stable integration surface and reading
them requires understanding the split above — use `positionOf` and `totalLocked0G()` instead.

## Layout

```
src/            contracts
script/deploy/  the chain work, as abstract contracts: IAIDeployer (system wiring and mock
                collateral), AccountFunder, UpgradeChecker — plus the thin *.s.sol shells
                that read parameters and write results back
script/         Upgrade.s.sol, Handover.s.sol — beacon upgrades and the governance handover
script/curve/   gen_exponential_table.py — derives the exponential curve's table from its
                parameters, writes it into the record, and re-checks it
deployments/    per-network parameters *and* the addresses a run produced
test/unit/      per-function behaviour, golden vectors, revert and permission matrices, and
                the scripts' chain work. Never touches the filesystem.
test/unit/curves/  the curves themselves, plus CurveConformance.t.sol — the abstract suite
                every curve must inherit and pass before the vault may point at it
test/sim/       seeded randomized simulation against an independent shadow model
test/script/    the file half of the scripts: parameters in, addresses out
docs/           frontend integration guide
run.sh upgrade.sh handover.sh faucet.sh verify.sh   operator wrappers
```

`deployments/iai-<chainId>.json` is both the input and the record: hand-written parameters go in, the
addresses of what was deployed come back into the same file. `iai-example.json` is the template.

## Build and test

```bash
forge test                      # the full suite, a few seconds
SIM_LONG=1 forge test --match-test test_Sim_Long   # 100k-operation simulation
python3 script/curve/gen_exponential_table.py deployments/iai-example.json --check   # the table is its parameters'
```

The unit fixture builds the system by calling the deployment script's own `IAIDeployer`, so every
test run is also a rehearsal of the real deployment, including its post-deploy sanity checks. A
fixture that re-implemented the wiring would let the two drift, and the suite could stay green
against a topology the script no longer produces.

## Deploy, upgrade, hand over

The procedures, and what each step checks, are in `docs/deployments.md`; so is everything about the
deployment record. The short form:

```bash
cp .env.example .env                  # PRIVATE_KEY, TEST_MNEMONIC
cp config.example.sh config.sh        # CHAIN_ID, RPC and the gas flags 0G needs; gitignored
$EDITOR deployments/iai-<chainid>.json   # start from iai-example.json
./run.sh genCurve     # derive the curve's table from the parameters in the record
./run.sh              # mock collateral (off mainnet), then the system
./run.sh status       # read it back
./run.sh unpause      # open issuance
./verify.sh           # publish sources to the 0G explorer

./upgrade.sh rehearse vault   # fork, upgrade there, compare state -- never skipped
./upgrade.sh vault            # only after the rehearsal passes

./handover.sh grant           # roles and beacons to their targets; deployer keeps its roles
./handover.sh renounce        # stand the deployer down, once the targets answer
./handover.sh renounce --keep vault-pauser,registry-pauser
                              # ...or leave it able to close the entrance and nothing else
```

**The vault deploys paused.** Opening issuance is a separate, explicit transaction — the only
launch-timing control the system has, and deliberately manual. The `CreditRegistry` deploys open;
nobody can stake before iAI exists. Deployment grants `DEFAULT_ADMIN_ROLE`, `PAUSER_ROLE` and
beacon ownership to the deploying account, and `PAUSE_EXEMPT_MINTER_ROLE` to nobody.

| Role | Intended holder | Can do |
| --- | --- | --- |
| `DEFAULT_ADMIN_ROLE` | multisig | grant and revoke roles, `setFoundation`, and — this is what makes it upgrade-grade — `setCurve` (which also moves the ceiling) and `setHarvestShare` |
| `PAUSER_ROLE` | guardian | close and open issuance and staking; `harvest` is pause-gated too, so it can also withhold the sweep. It cannot move funds, reprice or grant — a lighter key, because speed matters more than ceremony, and the one the handover can leave on a hot key with `--keep` |
| `PAUSE_EXEMPT_MINTER_ROLE` | nobody by default | `mint` while issuance is paused — same price, same ceiling, same slippage bound, same recipient. Granted per operation and revoked after; not part of the handover |
| beacon owner | multisig + timelock | upgrade one contract; each has its own beacon |

`PAUSE_EXEMPT_MINTER_ROLE` lets one nominated address mint while issuance is paused, on exactly the
same terms as any other mint. It exists because the vault's two states were otherwise "closed to
everyone" and "open to everyone", and admitting a single address meant unpausing and re-pausing
around the transaction. Granted for one operation and revoked after, so it is not part of the
handover. What it costs is accepted risk R10 in `docs/accepted-risks.md`: while it is held,
`pause()` no longer stops issuance at a manipulated oracle rate.
