# Blast Radius entry

Picks which sqlglot tests to run for each patch and splits them over workers.
Built with the help of Claude (AI use is allowed by the track rules).

1. Map (from the pinned commit): per test, lines of sqlglot run, classes used, files opened.
2. AST diff of each patch: comment-only edits are no-ops; real edits map to statements, lambdas or class attributes.
3. Cheap candidate tests are run for real on a patched overlay; only failures are kept.
4. LPT split over 1..8 workers, picking the cheapest worker count.

No answers are looked up by patch. sample.tar.gz holds the starter kit's sample set, used only by CI.
