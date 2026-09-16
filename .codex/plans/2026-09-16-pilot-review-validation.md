# Pilot review validation

Owner: Codex; source main 3ea044cf521296fe70a608dc64d4d03c338479ea.

The existing readiness projection separates guest_pilot recording from technical_rehearsal. Existing synthetic tests do not establish real participant evidence. The external pilot remains owned by issue #9.

Review count validation accepted booleans as integers. Six added input cases reject true and false for decisions_count, coverage_met, and coverage_total before storage. The unfixed test reached store.insert instead of rejecting; the repair passes 127 focused receipt, partnership, and pilot-service tests. No live receipt or participant state changed.

Full done.sh verification is the integration predicate. Retain actual external participation, custody, and correspondence in their private owner.
