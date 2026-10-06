/* Charts for RAT: Chart.js wrappers (churn over time, ownership doughnut)
   and a hand-rolled squarified treemap rendered as inline SVG. */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var instances = new Map(); // canvas -> Chart

  if (window.Chart) {
    Chart.defaults.color = "#8fa1b8";
    Chart.defaults.borderColor = "rgba(42, 54, 72, 0.55)";
    Chart.defaults.font.family =
      '"Segoe UI", system-ui, -apple-system, Roboto, sans-serif';
  }

  // ------------------------------------------------------------- helpers --
  function destroyChart(canvas) {
    var chart = instances.get(canvas);
    if (chart) {
      chart.destroy();
      instances.delete(canvas);
    }
  }

  function showEmpty(canvas, message) {
    destroyChart(canvas);
    var box = canvas.parentElement;
    box.querySelectorAll(".chart-empty").forEach(function (el) { el.remove(); });
    canvas.classList.add("hidden");
    var note = document.createElement("p");
    note.className = "muted small chart-empty";
    note.textContent = message;
    box.appendChild(note);
  }

  function prepare(canvas) {
    var box = canvas.parentElement;
    box.querySelectorAll(".chart-empty").forEach(function (el) { el.remove(); });
    canvas.classList.remove("hidden");
    destroyChart(canvas);
    return canvas.getContext("2d");
  }

  function compact(value) {
    var abs = Math.abs(value);
    if (abs >= 1e6) return (value / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (abs >= 1e3) return (value / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return String(value);
  }

  function bucketStep(points) {
    return points.length > 1 ? points[1].t - points[0].t : 86400;
  }

  function bucketFormat(points) {
    var step = bucketStep(points);
    if (step <= 2 * 86400) return "daily";
    if (step <= 20 * 86400) return "weekly";
    return "monthly";
  }

  // ------------------------------------------------------- churn over time -
  function renderChurn(canvas, points) {
    if (!points || !points.length) {
      showEmpty(canvas, "No commits in this selection.");
      return;
    }
    var step = bucketStep(points);
    var monthly = step > 20 * 86400;
    var labels = points.map(function (point) {
      var date = new Date(point.t * 1000);
      return monthly
        ? date.toLocaleDateString(undefined,
            { month: "short", year: "numeric" })
        : date.toLocaleDateString(undefined,
            { month: "short", day: "numeric" });
    });

    var ctx = prepare(canvas);
    instances.set(canvas, new Chart(ctx, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Added",
            data: points.map(function (p) { return p.added; }),
            backgroundColor: "rgba(102, 187, 106, 0.85)",
            stack: "churn",
            borderRadius: 2,
          },
          {
            label: "Removed",
            data: points.map(function (p) { return -p.removed; }),
            backgroundColor: "rgba(239, 83, 80, 0.85)",
            stack: "churn",
            borderRadius: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 14 },
          },
          y: {
            stacked: true,
            ticks: {
              callback: function (value) { return Math.abs(value); },
            },
          },
        },
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12 } },
          tooltip: {
            callbacks: {
              label: function (item) {
                return item.dataset.label + ": " + Math.abs(item.parsed.y);
              },
            },
          },
        },
      },
    }));
  }

  // ------------------------------------------------------- ownership donut -
  function renderOwnership(canvas, authors) {
    var rows = (authors || []).filter(function (a) { return a.churn > 0; });
    if (!rows.length) {
      showEmpty(canvas, "No churn to attribute in this selection.");
      return;
    }
    var top = rows.slice(0, 8);
    var rest = rows.slice(8);
    var labels = top.map(function (a) { return a.name; });
    var data = top.map(function (a) { return a.churn; });
    if (rest.length) {
      labels.push("Other (" + rest.length + ")");
      data.push(rest.reduce(function (sum, a) { return sum + a.churn; }, 0));
    }
    var palette = ["#4dd0e1", "#66bb6a", "#ffb74d", "#ef5350", "#ba68c8",
                   "#4fc3f7", "#aed581", "#f06292", "#546e7a"];

    var ctx = prepare(canvas);
    instances.set(canvas, new Chart(ctx, {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [{
          data: data,
          backgroundColor: palette,
          borderColor: "#1a2331",
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "56%",
        plugins: {
          legend: {
            position: "right",
            labels: { boxWidth: 12, font: { size: 10 } },
          },
          tooltip: {
            callbacks: {
              label: function (item) {
                var total = item.dataset.data.reduce(function (s, v) {
                  return s + v;
                }, 0);
                var share = total ? (item.parsed / total) * 100 : 0;
                return item.label + " - churn " + compact(item.parsed) +
                  " (" + share.toFixed(1) + "%)";
              },
            },
          },
        },
      },
    }));
  }

  // -------------------------------------------------------------- treemap --
  function growthColor(growth, churn) {
    var ratio = churn > 0 ? Math.max(-1, Math.min(1, growth / churn)) : 0;
    var t = (ratio + 1) / 2;               // 0 = pure removal, 1 = pure growth
    var hue = 4 + t * 138;
    var saturation = 32 + Math.abs(ratio) * 28;
    var lightness = 26 + Math.abs(ratio) * 10;
    return "hsl(" + hue + ", " + saturation + "%, " + lightness + "%)";
  }

  /* Squarified layout (Bruls et al.); returns rects in pixel space. */
  function squarify(entries, width, height) {
    var nodes = entries.slice().sort(function (a, b) {
      return b.value - a.value;
    });
    var out = [];
    var total = nodes.reduce(function (sum, n) { return sum + n.value; }, 0);
    if (!(total > 0) || width <= 0 || height <= 0) return out;

    var x0 = 0, y0 = 0, x1 = width, y1 = height;
    var value = total;
    var i0 = 0;
    var n = nodes.length;

    while (i0 < n) {
      var dx = x1 - x0;
      var dy = y1 - y0;
      var sumValue = nodes[i0].value;
      var minValue = sumValue, maxValue = sumValue;
      var alpha = Math.max(dy / dx, dx / dy) / value;
      var beta = sumValue * sumValue * alpha;
      var worst = Math.max(maxValue / beta, beta / minValue);
      var i1 = i0 + 1;

      for (; i1 < n; i1 += 1) {
        var v = nodes[i1].value;
        var nextSum = sumValue + v;
        var nextMin = Math.min(minValue, v);
        var nextMax = Math.max(maxValue, v);
        var nextBeta = nextSum * nextSum * alpha;
        var nextWorst = Math.max(nextMax / nextBeta, nextBeta / nextMin);
        if (nextWorst > worst) break;
        sumValue = nextSum;
        minValue = nextMin;
        maxValue = nextMax;
        worst = nextWorst;
      }

      var row = nodes.slice(i0, i1);
      if (dx < dy) {                       // lay the row across the full width
        var rowH = dy * sumValue / value;
        var kx = (x1 - x0) / sumValue;
        var x = x0;
        for (var i = 0; i < row.length; i += 1) {
          out.push({ item: row[i].item, x: x, y: y0,
                     w: row[i].value * kx, h: rowH });
          x += row[i].value * kx;
        }
        y0 += rowH;
      } else {                             // lay the row down the left side
        var rowW = dx * sumValue / value;
        var ky = (y1 - y0) / sumValue;
        var y = y0;
        for (var j = 0; j < row.length; j += 1) {
          out.push({ item: row[j].item, x: x0, y: y,
                     w: rowW, h: row[j].value * ky });
          y += row[j].value * ky;
        }
        x0 += rowW;
      }
      value -= sumValue;
      i0 = i1;
    }
    return out;
  }

  function clipText(name, maxChars) {
    if (maxChars < 3) return "";
    if (name.length <= maxChars) return name;
    return name.slice(0, Math.max(maxChars - 1, 0)) + "\u2026";
  }

  var TREEMAP_LIMIT = 60;

  function renderTreemap(container, items, onSelect) {
    container.textContent = "";
    var rows = (items || [])
      .filter(function (it) { return it.churn > 0; })
      .sort(function (a, b) { return b.churn - a.churn; });
    if (!rows.length) {
      var empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = "No churn on this object in the selected commit set.";
      container.appendChild(empty);
      return;
    }

    var width = Math.max(container.clientWidth || 600, 200);
    var height = Math.max(container.clientHeight || 320, 120);
    var shown = rows.slice(0, TREEMAP_LIMIT);
    var laid = squarify(shown.map(function (item) {
      return { value: item.churn, item: item };
    }), width, height);

    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("preserveAspectRatio", "none");

    laid.forEach(function (cell) {
      var pad = 1.5;
      var item = cell.item;
      var rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", String(cell.x + pad));
      rect.setAttribute("y", String(cell.y + pad));
      rect.setAttribute("width", String(Math.max(cell.w - pad * 2, 0.5)));
      rect.setAttribute("height", String(Math.max(cell.h - pad * 2, 0.5)));
      rect.setAttribute("fill", growthColor(item.growth, item.churn));

      var tip = document.createElementNS(NS, "title");
      tip.textContent = item.path + "\n" +
        "kind: " + item.kind + "\n" +
        "added: " + item.added + "  removed: " + item.removed + "\n" +
        "growth: " + item.growth + "  churn: " + item.churn + "\n" +
        "modifications: " + item.modifications;
      rect.appendChild(tip);
      rect.addEventListener("click", function () {
        if (onSelect) onSelect(item);
      });
      svg.appendChild(rect);

      if (cell.w >= 58 && cell.h >= 22) {
        var label = document.createElementNS(NS, "text");
        label.setAttribute("x", String(cell.x + pad + 5));
        label.setAttribute("y", String(cell.y + pad + 14));
        label.textContent = clipText(item.name,
          Math.floor((cell.w - 12) / 6.3));
        if (label.textContent) svg.appendChild(label);
      }
      if (cell.w >= 58 && cell.h >= 40) {
        var sub = document.createElementNS(NS, "text");
        sub.setAttribute("class", "sub");
        sub.setAttribute("x", String(cell.x + pad + 5));
        sub.setAttribute("y", String(cell.y + pad + 28));
        sub.textContent = "churn " + compact(item.churn);
        svg.appendChild(sub);
      }
    });

    container.appendChild(svg);
    if (rows.length > shown.length) {
      var note = document.createElement("p");
      note.className = "muted small";
      note.textContent = "Showing the " + shown.length + " largest of " +
        rows.length + " objects by churn.";
      container.appendChild(note);
    }
  }

  window.RATCharts = {
    renderChurn: renderChurn,
    renderOwnership: renderOwnership,
    renderTreemap: renderTreemap,
    bucketFormat: bucketFormat,
    compact: compact,
  };
})();
