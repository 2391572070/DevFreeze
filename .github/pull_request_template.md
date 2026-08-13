## Summary

<!-- What problem does this change solve, and why is this approach appropriate? -->

## Related issue

<!-- Use "Closes #123" when applicable. Behavior and format changes should have a design issue first. -->

## Verification

<!-- List the exact checks you ran and any platform-specific results. -->

## Security and privacy impact

<!-- Address command execution, secrets, paths, process signaling, Git/source mutation, network access, and persisted formats. Write "None" only after reviewing these boundaries. -->

## Checklist

- [ ] The change is focused and does not mix unrelated refactoring.
- [ ] Tests fail before and pass after the behavior change, or the reason tests are not needed is explained.
- [ ] `make check` passes locally.
- [ ] User-visible behavior is documented in both English and Chinese READMEs where applicable.
- [ ] Persisted-format changes update the schema, architecture, compatibility tests, and changelog without silently reinterpreting an existing schema version.
- [ ] Platform differences for Linux, macOS, and Windows are documented and tested where applicable.
- [ ] The change preserves preview-first recovery, exact argument arrays with `shell=False`, and the prohibition on capturing secrets or uncommitted file contents.

Do not use a pull request to disclose an uncoordinated vulnerability. Follow the
[private reporting policy](https://github.com/2391572070/DevFreeze/security/policy).
