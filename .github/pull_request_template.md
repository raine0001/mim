## Summary

- What changed:
- Why:

## Validation

- [ ] Tests added/updated where appropriate
- [ ] Local validation command(s) run and results noted

## Safe-Lane Contract Check (TOD Catch-Up Window)

- [ ] This PR is `safe-lane` (contract-neutral for MIM<->TOD shared packets)
- [ ] No changes to `runtime/shared` packet schemas or required field names
- [ ] No new trigger vocabulary consumed by TOD listeners
- [ ] No ACK correlation semantic changes
- [ ] If any item above is false, change is deferred to post-catchup queue

## Risk

- Risk level: low / medium / high
- Rollback plan:
