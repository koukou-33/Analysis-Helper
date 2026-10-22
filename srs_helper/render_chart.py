"""Private isolated Kaleido worker; invoked only by visual.render_png."""
import sys
from pathlib import Path

import kaleido
import plotly.io as pio


if __name__ == "__main__":
    figure = pio.from_json(sys.stdin.buffer.read().decode("utf-8"))
    content = kaleido.calc_fig_sync(figure,opts={"format":"png","width":1200,"height":750},
                                   kopts={"timeout":20,"mathjax":False})
    Path(sys.argv[1]).write_bytes(content)
