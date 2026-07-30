## v3.42.1 lessons (2026-07-29)
- **Assert every automated replace.** The v3.42.0 import insertion no-op'd
  on a fictional anchor and every printed check passed anyway. Postcondition
  asserts or it didn't happen.
- **Binding, not importability.** `import module` proves nothing about names
  used inside functions; smoke `hasattr(module, name)` for every name a new
  call site depends on.
- **Belt every new call site** in the send path: degraded feature > dead run.

