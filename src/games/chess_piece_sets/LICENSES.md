# Vendored chess piece sets

Each `<set>.json` maps python-chess piece symbols (`P`, `n`, …) to SVG `<g>`
fragments on the 45×45 grid `chess.svg.board()` expects. They were converted
from the individual piece SVGs in
[lichess-org/lila `public/piece/`](https://github.com/lichess-org/lila/tree/master/public/piece)
(fetched 2026-09-02): per-file CSS classes inlined, gradients flattened to
flat colors and SVG filters stripped (cairosvg renders both incorrectly when
pieces are instantiated per-square via `<use>`), internal ids namespaced per
piece, `xlink:href` rewritten to plain `href`, and the whole file wrapped in
a scaled `<g id="{color}-{piece}">`.

Licensing per lila's
[COPYING.md](https://github.com/lichess-org/lila/blob/master/COPYING.md):

| Set | Author | License |
|---|---|---|
| rhosgfx | RhosGFX | CC0 1.0 |
| fantasy | Maurizio Monge | MIT |
| spatial | Maurizio Monge | MIT |
| celtic | Maurizio Monge | MIT |
| kiwen-suwi | neverRare | CC BY 4.0 |
| totoy | Kosal Sen | CC BY 4.0 |
| merida | Armando Hernandez Marroquin | GPLv2+ |
| pixel | therealqtpi | AGPLv3+ |

The default set (cburnett, by Colin M.L. Burnett, GFDL/BSD/GPL) is not
vendored — it ships inside python-chess as `chess.svg.PIECES`.
