# Definition of done

A stage is done when a person could act on its output without asking you a
question. Concretely:

## Triage is done when

- The root cause names a **mechanism**, not a symptom. "Discount codes can be
  reused" is a symptom. "`apply_discount` appends to `order['discounts']` but
  never checks whether the code is already present, and `single_use` is
  declared on the code and read nowhere" is a mechanism.
- Every citation is real, quoted exactly, and shows something.
- What is *not* wrong is stated, where the ticket implied it was.
- Either there is a root cause, or there are specific questions. Never both
  vague and confident.

## A plan is done when

- Someone who has not read the analysis could implement it from the plan alone.
- The acceptance criteria are checkable by a person in under a minute each.
- The rollback is real. "Revert the commit" is real when nothing else changed;
  it is not real when the change writes to a database.
- Everything you considered and rejected is in `out_of_scope`, so the reviewer
  does not spend the gate wondering whether you thought of it.

## An implementation is done when

- It does what the plan said, in the files the plan named.
- It reads like the code around it — same naming, same error handling, same
  comment density. A reviewer should not be able to tell which function you
  wrote.
- The existing suite passes, unchanged.
- Any disagreement with the plan is in `deviations`, not in the code.

## Verification is done when

- Each test would **fail** against the original code. A test that passes either
  way is worse than none: it looks like coverage and will be counted as such.
- Each acceptance criterion is mapped to a test, or explicitly marked
  uncovered with a reason.
- The tests are about behaviour, not implementation. A test that asserts a
  private function was called pins the current design, and the next person to
  change it correctly will delete your test.

## Across all of it

Say what you do not know. The whole pipeline is built so that uncertainty
routes to a human instead of being resolved by confidence. An honest "I could
not establish this" is worth more than a plausible sentence, because a
plausible sentence is indistinguishable from a true one at the gate — and
that is exactly where someone is about to approve a code change on the
strength of it.
