# Prototype board — stripboard layout

Velleman ECL1/2 stripboard (39 × 31 holes, strips along the 80 mm side) carrying the
green MCP23017, the ADS1115 socket, the Pi header P, the cabinet box header J2 and the
expansion header J3. Paper page "4 · Prototype board", sheet PB3.

    PREV=1 PLACE="3 14 0 1 16 1" DYW=1.2 python3 stripboard_layout.py stripboard_layout.json
    python3 stripboard_render.py      # reads v3.json -> board.svg (rename paths as needed)

The layout script places the parts, derives the track cuts, routes wire links that never
overlap, and checks with a union-find that every strip piece carries exactly one net and
every net is one piece. Output: 73 pins, 26 cuts, 21 links.
