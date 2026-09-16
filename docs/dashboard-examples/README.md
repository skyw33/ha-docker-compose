# Dashboard examples

An example Lovelace dashboard for this integration (per-stack cards with
conditional show/hide via a helper entity) is planned for this directory
but not yet included in this initial publish.

It will depend on:

- `input_text.selected_stack` — a helper entity used to drive which
  stack's cards are shown.
- Three HACS frontend cards: [auto-entities](https://github.com/thomasloven/lovelace-auto-entities),
  [button-card](https://github.com/custom-cards/button-card), and
  [card_mod](https://github.com/thomasloven/lovelace-card-mod).

Every per-service entity in this integration exposes plain `stack` and
`service` attributes specifically to support a dashboard like this —
conditional cards can compare against `state_attr(entity_id, 'stack')`
directly, with no need to parse `entity_id` strings.
