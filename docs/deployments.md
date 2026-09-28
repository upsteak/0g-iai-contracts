# Deployment records, and running a deployment

How `deployments/iai-<chainId>.json` works, and the operator procedures that read and write it.
`CLAUDE.md` states the rules that constrain this file; what follows is the detail an operator needs
while actually running something.

## The record

`deployments/iai-<chainId>.json` is both the input and the output: hand-written parameters go in, and
the addresses a run produced come back to the same file. Start from `iai-example.json`.

**Tests cannot write into `deployments/`.** The default profile grants read-write only on
`./cache`, so a test that forgets to redirect a script fails with a permission error rather than
overwriting a deployment record. `run.sh` and `upgrade.sh` set `FOUNDRY_PROFILE=deploy`, which
differs from the default in `fs_permissions` alone — same solc, same `via_ir`, same optimizer, so
it cannot produce different bytecode. Run `forge script` by hand with that profile set.

**Write the whole document, never a single key.** Foundry's three-argument
`vm.writeJson(value, path, ".Key")` **silently does nothing** when the key does not already exist —
no error, no warning. Seed the output object from the current file and write it in one go:

```solidity
string memory obj = "iai";
vm.serializeJson(obj, json);              // keep everything already recorded
vm.serializeAddress(obj, "IAIVault", d.vault);
string memory finalJson = vm.serializeAddress(obj, "MintCurve", d.curve);
vm.writeJson(finalJson, path);            // only the LAST serialize call returns the document
```

Note the last line's comment: `vm.serializeXxx` returns the completed document only from the final
call, so capturing it early silently drops everything serialized afterwards.

**A rehearsal must not be able to write the real deployment record.** `./upgrade.sh rehearse`
calls the same entry point a real upgrade does, and that entry point records the implementation
address it just deployed — on a fork, an address that exists nowhere else. It used to write that
into the live record, replacing a working implementation with one that has no code, and nothing
said so until `./run.sh check` refused to pass. The rehearsal now redirects `DEPLOYMENT_PATH` to a
throwaway copy under `cache/`, so the snapshot and the record it produces both die with it. Any
future script that both forks and records needs the same treatment.

**Never `forge script --resume` a script that writes its own record.** These scripts write the
deployment file during *simulation*, before broadcasting. `--resume` re-simulates from scratch and
then sends only the transactions the previous broadcast never got to — so the record ends up naming
a fresh set of addresses that were never deployed, while the pending transactions land on the
previous set. It has happened: a run died on 0G's null-receipt flakiness after deploying every
contract but before three role grants, and the resume left the record pointing at phantoms while
correctly finishing the real system. `./run.sh check` caught it, which is what it is for.

Recover by reading the true addresses out of `broadcast/<Script>/<chainId>/run-<ts>.json` — the
`CREATE` entries carry `contractName` and `contractAddress` in deployment order — writing them back
into the record, and then running `./run.sh check` so the chain, not the file, has the last word.
The alternative, simply rerunning the whole deployment, is also fine and is usually quicker to
reason about.

**Curves are recorded by kind as well as by role.** A record carries `MintCurveKind` (which kind is
in force), `MintCurve` (its address), the address again under the kind's own name —
`ExponentialMintCurve` (the default) and `LinearMintCurve` — and `MintCurveHistory`, every
curve the record has ever named. `./run.sh setCurve <kind>` reads the kind key.

Each of those answers a different question, and conflating them has already caused two bugs:

- The **kind key holds the newest curve of that kind**, not the active one. `deployCurve` and
  `setCurve` are two steps on purpose, and between them the two disagree — so **`checkDeployment`
  must compare the active curve against `MintCurve` only.** Requiring it to equal the kind key made
  `./run.sh check` fail by construction in the window where an operator most wants to inspect a
  curve before putting it in service.
- A same-kind redeploy overwrites the kind key, which is why `MintCurveHistory` exists. A superseded
  curve still priced real mints and is still live on chain; reconciling those mints needs its
  address.

**A curve's constructor cannot validate its own parameters, and must not pretend to.** `slope` is
derived *from* `target`, so composing `deriveSlope` with `lockedAt` returns the target it started
from — every triple that survives `deriveSlope` hits its own target by construction. The
consistency check in `LinearMintCurve`'s constructor is therefore provably unreachable from any
caller, and that is fine: what it guards is that the two formulas stay inverses of one another.
Edit either so they stop agreeing and the next deployment fails instead of shipping a curve whose
published `target` is not the 0G it accounts for.

Its tolerance is `LinearCurveMath.maxFlooringGap(cap)` — the exact quantum a single flooring step
can lose, `cap^2 / (2*WAD^2) + 1` — and never a round number. The quantum is quadratic in the
anchor: about 4.3e7 wei-0G at 9,270 iAI and 5e15 at 100,000,000 iAI. A constant picked for one
anchor is blind at the other or rejects well-formed curves whose relative error is around 1e-19.
The bound lives beside the formulas it is derived from, so the golden vectors cover it and there is
only one definition of `WAD`.

**A curve's parameters belong to the curve, and the record says so.** Everything a curve needs to
be constructed sits under `CurveParams.<Kind>` — for the linear curve, `R0`, `AnchorCap` and
`Target`; for the exponential curve, `Base`, `Exponent`, `Target`, `BucketWidth`, `Top`, the
371-entry `Prices` array and, when it is not 1, `Power`. This is the one nested object in an otherwise flat file, and it earns the
exception: those keys are meaningless to any other curve, and each kind has its own block rather
than piling more top-level keys into a shared namespace. There is no top-level `Cap` key: the vault
has no ceiling of its own. Each curve's ceiling is in its own block -- `Top` for the exponential
curve, `AnchorCap` for the linear one. `HarvestShare` stays at the
top level because it governs how the collateral's yield is divided, which has nothing to do with
what any curve charges to issue.

`HarvestShare` records only the value in force, not the history. The vault keeps its own epochs, so
a position settled under an older split is restated on chain rather than reconstructed from a file;
`checkDeployment` holds the record against the chain, and `./run.sh setHarvestShare` moves both and
then reads the chain back.

**`Prices` is derived, never edited.** `./run.sh genCurve` runs `script/curve/gen_exponential_table.py`,
which derives the table from `Base`, `Exponent`, `Power`, `Target`, `BucketWidth` and `Top` alone,
as `Base * e^(Exponent * (s / Target)^Power)` at each bucket's upper bound -- 60-digit
`decimal`, each price rounded up to the wei, each bucket priced at its upper bound, and
`ceil(Top / BucketWidth)` buckets, the smallest table that covers the ceiling -- and writes the
block. **`Top` is the supply ceiling.** It goes into the constructor beside the table, and the
contract refuses a table that ends below it or runs a whole bucket or more past it. `./run.sh check`
and `./run.sh deployCurve` run the same script in `--check` mode first, so a table that disagrees
with the parameters beside it -- in any entry, or in its length -- cannot be deployed or pass a
check; `checkDeployment` then compares the deployed curve under the kind key against the record's
`Top` and table, entry by entry.

**That second comparison is only fatal while the exponential curve is in force.** `genCurve`
deliberately leaves the record ahead of the chain until `deployCurve` catches it up, so a record
that runs ahead is the documented procedure, not a fault. When some other curve is pricing,
`checkDeployment` warns and passes -- the only thing out of step is a dormant contract. When the
exponential curve *is* pricing, the record no longer describes the table every mint is charged
against and the check fails, as do a missing parameter block and a missing address. `setCurve`
refuses either way: it is about to make that curve price things. Every integer in the block is WAD-scaled and stored as a decimal string like the
rest of the file — `Exponent` is `4711000000000000000` for 4.711, `Power` `3274000000000000000` for 3.274, `Top` is 9,270 iAI in wei. A block without `Power` is read as 1, the pure exponential every table was generated with before the key existed; `genCurve` writes the key only when it is not 1, so those blocks are regenerated byte for byte. The unit tests cannot read the record, so `--solidity` also emits
`test/unit/curves/ExponentialTable.sol` as a mirror of **`iai-example.json`** -- the shipped
parameters, which is what the unit tests pin -- and `test/script/Deploy.t.sol` asserts the two
agree. Regenerate the mirror only when the *example's* parameters change. A network record's
parameters (`iai-16661.json`, `iai-16602.json`) may move without touching any test: their tables
are guarded by `run.sh check`, not by `forge test`, exactly as their addresses are. The golden vectors in
`test/unit/curves/ExponentialMintCurve.t.sol` were computed with `mpmath`, independently of the
generator, and may not be edited to follow it.

**The exponential curve's ceiling is its `top`, a constructor argument.** `maxSafeSupply()` is
`top` (9,270 iAI for the shipped parameters, covered by 371 buckets of 25 iAI that run 5 iAI past
it), and the vault issues nothing past it. `top` need not be a multiple of the bucket width; the last
bucket is simply cut off at the ceiling. Moving the ceiling is therefore always `genCurve --top ...`
→ `deployCurve` → `setCurve`, in that order, and the ceiling moves at the last step; a lower ceiling
is put in force the same way and brings the ceiling down with it, into burn-only mode if it is below
the live supply. Both paths are exercised in `test/script/Deploy.t.sol`. `run.sh setCurve ExponentialMintCurve` pre-flights the
kind key against the record's table before broadcasting, so a `genCurve` without `deployCurve` is
caught before a governance transaction is spent. The test fixture stays on the linear curve: its
closed form keeps the fuzz and split tests fast, and its anchor gives the fixture a ceiling of 9,270
iAI that tests can mint up to. Exponential coverage lives in its own suite, in `CurveSwap.t.sol`, in
`Deploy.t.sol` and in the simulation's alternating swaps.

`AnchorCap` is the supply a linear curve's slope was derived against, burned into the curve at
construction, and it is that curve's ceiling. It once shared a record key with the vault's cap, so
deploying a curve after any cap change silently derived a *different* curve from the same published
`R0` and `Target` — double the anchor and the slope came out 271850478687441015 instead of
2021598247004348741, with every number involved still looking plausible. The vault's cap is gone;
the lesson stays: **a curve is built from its own block and nothing else.**

The same split runs through the code: `Config` carries only what every deployment needs, each curve
kind gets its own parameter struct (`LinearCurveParams`, `ExponentialCurveParams`), and `IAIDeployer`
exposes one typed `_deploy<Kind>Curve` rather than one function switching on a name. A name-switched deployer has to accept the union of every
curve's parameters, so each new curve widens a struct the others then carry fields they have no use
for — and a caller filling in the wrong subset gets a curve that constructs cleanly and prices
differently. The name-to-parameter-shape mapping lives in exactly one place, `_curveOfKind` in
`IAI.s.sol`, because that is the half that reads records.

**Deploy scripts that touch collateral must be idempotent.** `Mock.s.sol` reuses an already-recorded
`MockA0G` instead of deploying a new one. An unconditional redeploy is silent and total: every
balance ever minted stays in the old token while the new system points at an empty one, nothing
reverts, and on a testnet with funded accounts it destroys all of them. Redeploying the *system*
against existing collateral is a supported operation and is how the testnet gets a rebuilt vault
without re-funding accounts.

There is one way past that guard, `./run.sh redeployMock`, for the case the guard cannot serve:
the mock itself has to change shape. It is a separate named entry point rather than a flag,
because nothing should reach it by rerunning a deployment, and it prints what it abandons before
it does anything. Using it commits you to the rest of the sequence — the vault caches its
collateral address at `initialize` and has no setter, so a new token means a new vault, which
means the whole system is redeployed and the accounts refunded from the new token.

**A mock's share price comes from the oracle, never from what it holds.** `MockA0G` is an ERC-4626
over W0G, and the only conversion input it overrides is `totalAssets() = totalSupply() *
oracle.getValue() / 1e18` — which is exactly, and only, what the real token (Mellow's
`SourceCore`) overrides. Everything else is OpenZeppelin's and follows from that one number.
Deriving the price from the balance held instead would break an identity that holds on mainnet:
`oracle.getValue()`, `convertToAssets(1e18)` and `totalAssets/totalSupply` are all the same
number there, because W0G is one-for-one with 0G. A caller sizing a deposit from `previewDeposit`
would then be handed an amount the vault values differently. It also means the unrestricted
faucet is harmless to the accounting: minting shares with nothing behind them leaves
`totalAssets` consistent, because it was never counting the balance.

## Running a deployment

```bash
cp .env.example .env                  # PRIVATE_KEY, TEST_MNEMONIC
cp config.example.sh config.sh        # CHAIN_ID and RPC; gitignored
                                      # (needs python3 >= 3.9 for the curve table; standard library only)
$EDITOR deployments/iai-<chainid>.json   # start from iai-example.json
./run.sh genCurve     # derive the exponential curve's table from the parameters in the record
                      # (pass --base/--exponent/--target/--width/--top to change them; the
                      # table is never edited by hand, and `check` re-derives and compares it)

./run.sh              # mock collateral (off mainnet), then the system
./run.sh accounts     # testnet: derive and fund the account set
./run.sh status       # read it back
./run.sh unpause      # open issuance
./verify.sh           # publish sources to the 0G explorer
```

`config.sh` carries the gas flags every 0G transaction needs — `--slow --with-gas-price 3gwei
--priority-gas-price 3gwei`, since 0G's EIP-1559 wants both pinned and `--slow` stops a nonce gap
from stranding the rest of a deployment.

Changing the curve on a live network is three commands, in this order: `./run.sh genCurve ...`
rewrites the table in the record (`--top` sets the ceiling and sizes the table to cover it), `./run.sh deployCurve
ExponentialMintCurve` deploys it and records the address under its kind, and `./run.sh setCurve
ExponentialMintCurve` puts it in service. Nothing already minted is repriced, and the supply ceiling
becomes the new curve's `top` at the last step — there is no separate cap to move before or after.

Running `forge script` by hand works too, but set **`FOUNDRY_PROFILE=deploy`**: under the default
profile `deployments/` is read-only, so that a test which forgets to redirect a script fails with a
permission error instead of overwriting a deployment record. See `run.sh` for the exact invocations.

**The vault deploys paused**, so `./run.sh unpause` is the launch. The `CreditRegistry` deploys
open; nobody can stake before iAI exists. Deployment grants `DEFAULT_ADMIN_ROLE`, `PAUSER_ROLE` and
beacon ownership to the deploying account, and `PAUSE_EXEMPT_MINTER_ROLE` to nobody — that one is
granted per operation with `./run.sh grantPausedMinter <addr>`, revoked with `revokePausedMinter`,
and never handed over, since it is not a seat. Its cost to `pause()` is R10 in
`accepted-risks.md`; README explains what the role is for.

## Handing over governance

Fill in `Admin`, `Guardian` and `BeaconOwner` in the deployment file. None of the three is read
during a deployment — they are read only here — so a wrong address in them surfaces at `grant` and
nowhere earlier. Check them against the chain before running it.

```bash
./handover.sh status      # who holds what right now
                          # — execute something from the Safe, and see it land —
./handover.sh grant       # every role and beacon to its target; the deployer keeps its roles
./handover.sh status      # confirm
./handover.sh renounce    # stand the deployer down
```

Two transactions, deliberately — but only the roles are split across them. `grantRole` adds a
holder, so after `grant` each role is held by both the target and the deployer, and the targets can
be read back before the last key that could put one back is given up. `renounce` re-reads
governance from the chain and refuses unless the targets already hold everything — a mistyped
address stops there, with the deployer still in control, rather than after, with nobody in control.

**The beacons are not split across the two steps: `grant` moves them.** `Ownable` has a single
owner, no acceptance step and no admin override, so the deployer loses the upgrade key the moment
`grant` returns and a wrong `BeaconOwner` is already beyond recovery — `renounce`'s precondition can
only report it. **So confirm the Safe responds before `grant`, not only between the two steps.** A
wrong `Admin` is survivable while the deployer still holds admin; a wrong `BeaconOwner` is not
survivable at all.

### Keeping a role on the deployer

`renounce` stands the deployer down completely by default. `--keep` leaves named roles behind:

```bash
./handover.sh renounce --keep vault-pauser,registry-pauser
```

The names are `iai-admin`, `vault-admin`, `registry-admin`, `vault-pauser` and `registry-pauser`.
Anything else is an error rather than a skipped word — the list is typed once, by hand, to drive a
transaction that cannot be undone, and `--keep vault-pausers` quietly renouncing the pauser it was
written to save is not a failure mode this gets to have. Spaces are stripped, so a quoted
`"a, b"` is the same list as `a,b`.

`PAUSE_EXEMPT_MINTER_ROLE` cannot be named and is always given up. It is not a seat but a permission
granted per operation, and a stood-down key that can still mint through the pause it just applied is
the thing a handover exists to rule out.

The retention worth making is the two pausers: closing the entrance has to be fast and a multisig is
not, and a pauser cannot grant, reprice or move a beacon, so keeping one gives up nothing
irreversible. Keeping an admin is a different matter — on the vault it is the upgrade-grade key the
handover is for, and on iAI it grants `MINTER_BURNER_ROLE`, so it mints without limit (R9).

**A retained pauser is only half of what R1 and R5 ask for.** Both responses are `pause()` *and*
revoking `PAUSE_EXEMPT_MINTER_ROLE`, and the second needs `DEFAULT_ADMIN_ROLE`, which a pauser does
not have. That is survivable only because in steady state nobody holds the exemption: it is granted
for one operation and revoked at the end of it. Leave it granted and the fast key can no longer
complete the response on its own — closing the entrance would still leave that holder minting.
Revoke it as part of the operation that needed it, not as a follow-up.

What the command checks before it touches anything, all of which would otherwise fail confusingly
*after* the renouncing rather than clearly before it: that the deployer actually holds each role
named; that naming the deployer as `Guardian` or `Admin` is matched by keeping the matching roles,
since otherwise the run ends with nobody holding them, reported as the target address's fault rather
than the list's; and that the deployer is not its own `BeaconOwner`. The completion check then reads
every role in both directions — so a retention that silently did not take fails as loudly as one
that was not wanted — and says the same thing about the beacons independently, because that is where
"the deployer holds nothing beyond what it kept" is actually claimed, and the upgrade key is
something it can hold.

**A deployer that keeps the beacons is deliberately not expressible.** `--keep` has no name for the
upgrade key and the handover refuses a record that leaves it behind, so "roles to the multisig now,
beacons to the timelock later" is not a staging this supports. That is on purpose: admin and beacon
ownership have to land on the same multisig, since between `setCurve` and `setHarvestShare` admin
already carries the economic power an upgrade has. Staging the *roles* is supported — that is what
`--keep` is for.

Three spellings of the flag are refused rather than resolved: an empty list, a list of nothing but
separators, and a second `--keep`. Each has a quiet reading that gives up roles the operator wrote
the flag to save, and a complete stand-down is already available by leaving the flag off. The empty
case is refused in the script as well as in `handover.sh`, because an unset variable expands to `""`
and reaches `forge script` directly.

Renouncing only what is held, so a later run gives up what an earlier one kept:
`./handover.sh renounce` after a `--keep` finishes the job without disturbing anything else.

Other operator entrypoints: `./run.sh setHarvestShare <wad>`, `./run.sh harvestShare`,
`./run.sh pause`, `./run.sh harvest`, `./run.sh quote <amount>`,
`./run.sh mint <amount> <maxA0GIn>`, `./run.sh pausedMinter|grantPausedMinter|revokePausedMinter
<addr>`, and `forge script script/deploy/IAI.s.sol --sig "setFoundation(address)" <addr>`.

## Upgrades

Every contract sits behind its own `UpgradeableBeacon`, so one upgrade cannot reach the others.
Correctness is established by rehearsal on a mainnet fork, not by an on-chain self-check: a guard the
contract computes about itself is only sound while it reads the right storage slots, which is exactly
what is in doubt when a layout has shifted.

```bash
export CHECK_ACCOUNTS=0xLargestHolder,0xNextOne   # optional but recommended

./upgrade.sh rehearse vault    # forks the chain, upgrades there, compares state
./upgrade.sh vault             # only after the rehearsal passes
```

The rehearsal forks the configured chain, snapshots the curve address, every balance and the
positions named in `CHECK_ACCOUNTS`, upgrades, and reverts if anything moved. The curve address is
in there because pricing and the ceiling both live outside the beacon now: repointing it is the one
thing an upgrade can still do to reprice or re-cap the system. `iai` and `registry`
are the other two targets.
