# House policy — FILL THIS IN BEFORE YOU ROLL OUT

> This file is worth more than any prompt tuning you will do.
>
> Every unfilled placeholder below is a defect generator. A model that does not
> know your logging convention will invent one. A model that does not know your
> error-handling idiom will pick a reasonable one that is not yours, and the
> change will read as foreign to the maintainer who inherits it. A model that
> does not know which helpers already exist will write its own.
>
> Fill it in once, review it like code, and update it when the conventions move.

## The service

- **What it does:** TODO
- **Who runs it:** TODO
- **Blast radius of a bad change:** TODO — be honest here; it sets how
  conservative every plan should be.

## Language and layout

- **Language and version:** TODO (e.g. Python 3.11)
- **Where application code lives:** TODO (e.g. `src/`)
- **Where tests live, and how they are named:** TODO
- **How the test suite is run:** TODO (this must match `FOREMAN_TEST_COMMAND`)
- **Formatter and linter, and whether they are enforced:** TODO

## Conventions the change must match

- **Error handling:** TODO — do you raise, return a result type, or log and
  continue? Which exceptions are allowed to escape a request handler?
- **Logging:** TODO — which logger, which level for what, and what must never
  be logged (identifiers, payloads, anything personal).
- **Configuration:** TODO — where settings come from, and what happens when one
  is missing. State explicitly whether a missing setting should fail closed.
- **Money, time and identifiers:** TODO — the types, the rounding, the
  timezone. Most quiet defects in a service like this live here.
- **Concurrency:** TODO — what is allowed to be shared and mutable.

## Helpers that already exist — use these, do not reimplement them

- TODO: validation
- TODO: authorisation
- TODO: database access and transactions
- TODO: HTTP clients and retries
- TODO: serialisation

## Things that look wrong and are not

List the patterns a careful outsider would flag as defects but which are
deliberate here. Every entry you omit is a plan that proposes to "fix"
something working.

- TODO
- TODO

## Absolutely never

- TODO (e.g. never widen a database transaction across an HTTP call)
- TODO (e.g. never add a dependency; that is a separate, human decision)
