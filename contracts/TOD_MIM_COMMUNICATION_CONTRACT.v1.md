# TOD MIM Communication Contract v1

Purpose: give MIM one canonical, build-ready contract package for TOD communication so TOD consumes rules instead of guessing them.

## What This Package Is

This package contains exactly three files:

- machine-readable canonical contract: `contracts/TOD_MIM_COMMUNICATION_CONTRACT.v1.yaml`
- strict validator: `contracts/TOD_MIM_COMMUNICATION_CONTRACT.v1.schema.json`
- this companion document: `contracts/TOD_MIM_COMMUNICATION_CONTRACT.v1.md`

The YAML file is authoritative. This document explains how MIM should use it.

Communication-scoped authority rule:

- TOD↔MIM communication truth belongs on the MIM-owned server surface.
- The ARM Pi is an executor-side transport target, not the primary communication truth surface.
- Do not route contract authority, receipt authority, or general TOD↔MIM state authority through the ARM host.

## Non-Negotiable Rules

MIM owns the contract.

TOD consumes the contract.

Neither side may fork, reinterpret, or silently repair the contract.

If a rule is ambiguous, MIM must fix the contract before transmission.

If multiple writers or multiple truth surfaces remain possible, the contract is invalid.

## MIM Builder Workflow

### Phase 1: Build And Normalize

MIM must use the YAML contract as the canonical source for:

- transport layer definition
- message kinds and envelope rules
- task classification
- authority and decision weighting
- heartbeat behavior
- fallback behavior
- reconciliation behavior
- audit behavior
- mode behavior
- failure taxonomy

Builder rule:

- do not add a transport, message, or writer outside the YAML file
- do not rely on repo history or tribal knowledge as runtime truth
- do not preserve legacy parallel writers for convenience

### Phase 2: Canonicalization

Canonical artifact:

- `contracts/TOD_MIM_COMMUNICATION_CONTRACT.v1.yaml`

Validator:

- `contracts/TOD_MIM_COMMUNICATION_CONTRACT.v1.schema.json`

Companion:

- `contracts/TOD_MIM_COMMUNICATION_CONTRACT.v1.md`

Required properties of the canonical artifact:

- versioned
- self-contained
- deterministic top-level structure
- explicit authority per domain
- no implied behavior

### Phase 3: Self-Validation

MIM must not send the contract until it passes all required simulations embedded in the contract:

- request -> ack -> result happy path
- peer unavailable
- retry and idempotency
- state mismatch and reconciliation
- fallback activation
- superseded request ignored

MIM must verify:

- no ambiguous authority
- no multiple writers
- no conflicting flows
- exactly one ack and one result for one request
- no duplicate execution under retry

Stop rule:

- if any verification fails, stop, fix the contract or implementation, then revalidate

### Phase 4: Transmission To TOD

Transmission must be atomic and include:

- contract version
- checksum sha256 of the canonical YAML payload
- generation timestamp
- source identity

TOD must return a receipt confirming:

- receipt acknowledged
- checksum matches
- no reinterpretation will be applied

Suggested receipt surface:

- `runtime/shared/TOD_MIM_CONTRACT_RECEIPT.latest.json`

### Phase 5: Embedded TOD Directive

The YAML file already includes `TOD_IMPLEMENTATION_DIRECTIVE`.

This is not advisory text. It is implementation instruction.

TOD must:

- implement against the contract exactly
- report conflicts back to MIM
- avoid silent schema drift

TOD must not:

- redefine message schema
- create alternate transport paths
- introduce new writers for defined domains

### Phase 6: Migration

The YAML file includes a four-stage migration path:

- shadow mode
- cutover readiness
- cutover
- rollback

MIM should not cut over until these are true:

- single authority per domain confirmed
- shadow validation green
- TOD receipt checksum confirmed
- no parallel writer detected

### Phase 7: Success Criteria

The system is correct only when all of these are true:

- one request produces one ack and one result
- no duplicate execution occurs under retry
- both systems agree on objective, task, and request identity
- fallback works when primary is down
- mismatch triggers reconciliation, not drift
- no multiple latest writers exist
- no multiple truth surfaces exist
- authority is unambiguous per domain

## How MIM Should Supervise Itself

Before changing code, MIM should ask these questions in order:

1. Which artifact domain is this change touching?
2. Does the YAML already define the writer for that domain?
3. Does this change create a second writer, truth surface, or alternate transport?
4. Does this change alter request, ack, or result semantics without updating the contract?
5. Does this change weaken idempotency, supersedence, or reconciliation?

If any answer is unsafe, MIM must update the contract first or reject the implementation change.

## Recommended Implementation Sequence

1. Lock single-writer surfaces in code.
2. Lock shared envelope fields in producers and consumers.
3. Lock request, ack, result, heartbeat, and fallback packet types.
4. Add validator-backed contract check in CI or preflight.
5. Run the self-validation matrix.
6. Transmit the exact YAML payload to TOD with checksum.
7. Require TOD receipt before live cutover.

## Suggested Transmission Envelope

```json
{
  "packet_type": "tod-mim-contract-distribution-v1",
  "contract_name": "TOD_MIM_COMMUNICATION_CONTRACT",
  "contract_version": "v1",
  "generated_at": "<UTC ISO8601>",
  "source_identity": {
    "actor": "MIM",
    "host": "<host>",
    "service": "<service>",
    "instance_id": "<instance>"
  },
  "checksum_sha256": "<sha256 of canonical yaml bytes>",
  "artifact_name": "TOD_MIM_COMMUNICATION_CONTRACT.v1.yaml",
  "payload": "<exact canonical yaml content>"
}
```

## Validator Use

The schema file validates the contract package structure, not live execution packets.

Use it to enforce:

- required sections exist
- machine-readable structure remains stable
- the package does not accidentally lose key policy sections

Use packet-level tests separately to validate runtime behavior against the contract.

## Blunt Operational Rule

If MIM finds itself saying any of the following, the contract is not ready:

- “TOD can probably infer that.”
- “This behavior is implied by the old bridge.”
- “Both paths can exist for now.”
- “We can let TOD normalize it locally.”

Those are drift sources. Fix the YAML, then rebuild.
