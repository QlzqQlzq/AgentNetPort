# Public Source Snapshot

The public tree contains the Core, SDK, adapters, default plugin, WebUI,
examples, specifications and repeatable tests. It excludes machine state,
credentials, communication history, design reference images, old diagnostic
outputs and one-time migration or live-agent messaging scripts.

Documentation paths and Windows account examples use placeholders.
`tools/run_regression.py` resolves the checkout location dynamically and returns
a nonzero exit status when a suite fails.
The WebUI suite count is 30; the runner's older expectation of 29 is corrected.

Tests use disposable homes and local fake peers. Real vendor CLI sessions and
external providers still require installation-specific validation.

No new repository-wide license is granted by this publication. Preserve any
existing third-party notices and consult their upstream licenses separately.
