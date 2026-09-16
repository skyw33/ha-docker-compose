# Dashboard examples

[`docker_stacks_v3.yaml`](docker_stacks_v3.yaml) — an example Lovelace
dashboard for this integration: a stack overview grid plus a detail panel
for whichever stack is selected, showing its containers, current/pull
target/registry/GitHub-release versions, and compose config.

![Dashboard screenshot](dashboard.png)

It depends on:

- `input_text.selected_stack` — a helper entity that drives which
  stack's detail panel is shown; clicking a stack card sets it.
- Two HACS frontend cards:
  [auto-entities](https://github.com/thomasloven/lovelace-auto-entities)
  and [button-card](https://github.com/custom-cards/button-card).

The dashboard contains no hardcoded stack/service names — every card is
generated dynamically from `auto-entities` filters, so it adapts
automatically as stacks are added or removed. Selection matching is done
via the `stack`/`service` attributes every per-service entity in this
integration exposes, not by parsing `entity_id` strings.
