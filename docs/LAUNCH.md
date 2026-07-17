# Developer Preview Launch

## Goal

Recruit ten Hermes developers for a two-week preview. A successful tester
installs the plugin, exchanges a real message, tries a threaded or branched
conversation, and reports onboarding friction.

## Release gate

- [ ] Unit and packaging contract tests pass on Python 3.10–3.13.
- [ ] Contract smoke test passes against the current stable Hermes release and
      upstream `main`.
- [ ] A clean-machine native install completes without manually copying files.
- [ ] Hosted and self-hosted quickstarts each produce a first message.
- [ ] Live two-account round trip, branch context, attachment, catch-up, key
      revocation, and sender denial are verified.
- [ ] `v0.1.0` is tagged with release notes and known limitations.
- [ ] A 60–90 second install-to-branch demo is recorded.

## Discord announcement

Post in the Nous Research Discord `#plugins-skills-and-skins` channel:

> I built an fmsg platform plugin that gives a Hermes agent its own federated
> messaging address. fmsg threads map directly to Hermes sessions and branches,
> so the message tree is also the agent's conversation context. I'm looking for
> ten developers to test it over the next two weeks. There are hosted and fully
> self-hosted routes. Install with
> `hermes plugins install markmnl/hermes-fmsg --enable`.

Attach the short demo, link this repository, link both onboarding routes, state
the supported Hermes version, and keep feedback in one Discord thread. Do not
describe the plugin as bundled or officially maintained by Hermes.

## Feedback template

```text
Hermes version:
OS and Python:
Hosted or self-hosted:
Time to first message:
Workflow tested:
Expected result:
Actual result:
Sanitized logs or issue link:
Would you keep using it? Why/why not?
```

Never request API keys, JWTs, private message bodies, or unredacted addresses.
Move reproducible bugs to GitHub issues.

## Two-week cadence

- **Day 0:** announcement and first office-hour time.
- **Day 3:** publish fixes, known limitations, and remaining tester slots.
- **Day 7:** second office hour and midpoint retention check.
- **Day 14:** publish results, ship the next patch/minor release, and ask active
  testers for example workflows or testimonials they consent to share.

Target at least eight first-message successes, seven active testers after one
week, hosted median time-to-first-message under fifteen minutes, and no
lost-message, credential-exposure, or sender-authorization defects.

Do not add plugin telemetry. Use voluntary check-ins, issues, office hours, and
aggregate service health data. After the cohort, use the evidence to request an
upstream community/documentation listing while keeping the plugin standalone.
