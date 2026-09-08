# Changelog

<!-- <START NEW CHANGELOG ENTRY> -->

## Unreleased

- The profile list is Bifrost's catalog (`GET /api/v1/profiles`, project-scoped
  by the caller's token), and a start names the profile in `ClusterSpec.profile`
  instead of sending a shape. The built-in `small`/`medium`/`gpu` catalog on
  `rayproject/ray:2.9.0` and the `BifrostConfig.profiles` traitlet are gone: a
  Ray 2.56 notebook connecting to the cluster they produced was refused with a
  version mismatch.
- Stopped clusters no longer appear in the panel as "pending". Bifrost keeps a
  tombstone until it purges the record; the list shows what can be used.
- Requires `bifrost_client` 0.1.10.

<!-- <END NEW CHANGELOG ENTRY> -->
