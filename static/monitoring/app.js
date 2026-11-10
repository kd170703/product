(function () {
  'use strict';

  const toggle = document.querySelector('.mobile-toggle');
  const sidebar = document.getElementById('sidebar');
  if (toggle && sidebar) {
    toggle.addEventListener('click', () => {
      const open = sidebar.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        sidebar.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('click', (event) => {
      if (!sidebar.contains(event.target) && !toggle.contains(event.target)) {
        sidebar.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const periodMode = document.getElementById('period-mode');
  function setPeriodMode() {
    if (!periodMode) return;
    document.querySelectorAll('[data-period-modes]').forEach((element) => {
      const visible = element.dataset.periodModes.split(' ').includes(periodMode.value);
      element.hidden = !visible;
      element.querySelectorAll('input,select').forEach((input) => {
        input.disabled = !visible;
      });
    });
  }
  if (periodMode) {
    periodMode.addEventListener('change', setPeriodMode);
    setPeriodMode();
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const axisFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
  const scientificFormat = new Intl.NumberFormat('ru-RU', {
    notation: 'scientific',
    maximumFractionDigits: 2,
  });
  function formatAxis(value) {
    const formatted = axisFormat.format(value);
    return formatted.length > 14 ? scientificFormat.format(value) : formatted;
  }
  const dataFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 8 });
  const fallbackColors = [
    '#2455b4',
    '#087b71',
    '#aa5000',
    '#7144a3',
    '#a52e59',
    '#355e3b',
    '#75541d',
    '#2c627e',
  ];
  const dashPatterns = ['', '8 4', '2 4', '10 3 2 3', '12 4', '5 3', '2 3 7 3', '9 3 3 3'];
  const chartStates = new WeakMap();
  let chartSequence = 0;

  function svgElement(tag, attributes = {}, text) {
    const element = document.createElementNS(SVG_NS, tag);
    Object.entries(attributes).forEach(([key, value]) => {
      element.setAttribute(key, String(value));
    });
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function htmlElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function number(value) {
    if (value === null || value === undefined || value === '') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (error) {
      return '';
    }
  }

  function themeColor(name, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }

  function seriesColor(index) {
    const position = index % fallbackColors.length;
    return themeColor(`--chart-series-${position + 1}`, fallbackColors[position]);
  }

  function splitLines(value, maxCharacters) {
    const words = String(value || '')
      .trim()
      .split(/\s+/);
    const lines = [];
    let current = '';
    words.forEach((word) => {
      const pieces = [];
      for (let offset = 0; offset < word.length; offset += maxCharacters) {
        pieces.push(word.slice(offset, offset + maxCharacters));
      }
      pieces.forEach((piece) => {
        if (current && current.length + piece.length + 1 > maxCharacters) {
          lines.push(current);
          current = piece;
        } else {
          current = current ? current + ' ' + piece : piece;
        }
      });
    });
    if (current) lines.push(current);
    return lines.length ? lines : [''];
  }

  function multilineText(svg, lines, x, y, attributes = {}) {
    const text = svgElement('text', {
      x,
      y,
      fill: themeColor('--chart-axis', '#526277'),
      'font-size': 14,
      ...attributes,
    });
    lines.forEach((line, index) => {
      text.append(svgElement('tspan', { x, dy: index ? 18 : 0 }, line));
    });
    svg.append(text);
    return text;
  }

  function marker(index, x, y, color, size = 5) {
    const group = svgElement('g', {
      transform: `translate(${x} ${y})`,
      fill: '#ffffff',
      stroke: color,
      'stroke-width': 2.5,
      'stroke-linejoin': 'round',
      'data-series-index': index,
      'data-marker': [
        'circle',
        'square',
        'diamond',
        'triangle',
        'cross',
        'plus',
        'triangle-down',
        'hexagon',
      ][index % 8],
    });
    const shapes = [
      () => svgElement('circle', { cx: 0, cy: 0, r: size }),
      () => svgElement('rect', { x: -size, y: -size, width: size * 2, height: size * 2 }),
      () =>
        svgElement('polygon', {
          points: `0,${-size - 1} ${size + 1},0 0,${size + 1} ${-size - 1},0`,
        }),
      () =>
        svgElement('polygon', {
          points: `0,${-size - 1} ${size + 1},${size} ${-size - 1},${size}`,
        }),
      () =>
        svgElement('path', {
          d: `M${-size},${-size} L${size},${size} M${-size},${size} L${size},${-size}`,
          fill: 'none',
        }),
      () =>
        svgElement('path', {
          d: `M${-size - 1},0 H${size + 1} M0,${-size - 1} V${size + 1}`,
          fill: 'none',
        }),
      () =>
        svgElement('polygon', {
          points: `${-size - 1},${-size} ${size + 1},${-size} 0,${size + 1}`,
        }),
      () =>
        svgElement('polygon', {
          points: `${-size},${-size / 2} 0,${-size - 1} ${size},${-size / 2} ${size},${size / 2} 0,${size + 1} ${-size},${size / 2}`,
        }),
    ];
    group.append(shapes[index % shapes.length]());
    return group;
  }

  function contextText(chart) {
    return [
      chart.title,
      chart.unit && `Единицы: ${chart.unit}`,
      chart.territory && `Территория: ${chart.territory}`,
      (chart.period_summary || chart.period) && `Период: ${chart.period_summary || chart.period}`,
      `Источник: ${chart.source_name || 'исходный реестр'}`,
    ]
      .filter(Boolean)
      .join('. ');
  }

  function hideTooltip(state) {
    state.tooltip.hidden = true;
    state.points.forEach((point) => point.element.removeAttribute('aria-describedby'));
  }

  function showTooltip(state, element, text) {
    state.tooltip.textContent = text;
    state.tooltip.hidden = false;
    element.setAttribute('aria-describedby', state.tooltip.id);
    const pointBounds = element.getBoundingClientRect();
    const containerBounds = state.container.getBoundingClientRect();
    const tooltipBounds = state.tooltip.getBoundingClientRect();
    const halfWidth = tooltipBounds.width / 2;
    const desiredLeft = pointBounds.left - containerBounds.left + pointBounds.width / 2;
    const left = Math.max(
      halfWidth + 8,
      Math.min(state.container.clientWidth - halfWidth - 8, desiredLeft),
    );
    const top = Math.max(
      tooltipBounds.height + 12,
      pointBounds.top - containerBounds.top + state.container.scrollTop - 8,
    );
    state.tooltip.style.left = `${left}px`;
    state.tooltip.style.top = `${top}px`;
  }

  function inspectablePoint(state, element, label, value, series) {
    const text =
      `${label}. ${series}: ${dataFormat.format(value)} ${state.chart.unit || ''}`.trim();
    const index = state.points.length;
    element.append(svgElement('title', {}, text));
    element.setAttribute('tabindex', index === state.focusedPoint ? '0' : '-1');
    element.setAttribute('role', 'img');
    element.setAttribute('aria-label', text);
    element.classList.add('chart-data-point');
    element.addEventListener('mouseenter', () => showTooltip(state, element, text));
    element.addEventListener('mouseleave', () => {
      if (document.activeElement !== element) hideTooltip(state);
    });
    element.addEventListener('focus', () => {
      state.points.forEach((point) => point.element.setAttribute('tabindex', '-1'));
      element.setAttribute('tabindex', '0');
      state.focusedPoint = index;
      showTooltip(state, element, text);
    });
    element.addEventListener('blur', () => hideTooltip(state));
    element.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        hideTooltip(state);
        return;
      }
      let next;
      if (['ArrowRight', 'ArrowDown'].includes(event.key))
        next = Math.min(state.points.length - 1, index + 1);
      if (['ArrowLeft', 'ArrowUp'].includes(event.key)) next = Math.max(0, index - 1);
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = state.points.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        state.points[next].element.focus({ preventScroll: true });
      }
    });
    state.points.push({ element, index });
  }

  function buildAccessibleData(state) {
    const { chart, labels, datasets } = state;
    const auxiliary = htmlElement('div', 'chart-accessibility');
    if (datasets.length > 1) {
      const legend = htmlElement('ul', 'chart-legend');
      legend.setAttribute('aria-label', 'Обозначения рядов диаграммы');
      datasets.forEach((dataset, index) => {
        const item = htmlElement('li', 'chart-legend-item');
        const symbol = svgElement('svg', {
          width: 38,
          height: 22,
          viewBox: '0 0 38 22',
          'aria-hidden': 'true',
          focusable: 'false',
        });
        const color = seriesColor(index);
        symbol.append(
          svgElement('line', {
            x1: 2,
            y1: 11,
            x2: 36,
            y2: 11,
            stroke: color,
            'stroke-width': 3,
            'stroke-dasharray': dashPatterns[index % dashPatterns.length],
          }),
        );
        symbol.append(marker(index, 19, 11, color, 4));
        item.append(symbol, htmlElement('span', '', `${index + 1}. ${dataset.label}`));
        legend.append(item);
      });
      auxiliary.append(legend);
    }
    const caption = htmlElement(
      'p',
      'chart-caption',
      datasets.length > 1
        ? 'Ряды различаются маркерами, рисунком линий и номерами в легенде. Стрелки перемещают фокус между значениями; Escape скрывает подсказку.'
        : 'Значения доступны с клавиатуры: стрелки перемещают фокус между точками, Escape скрывает подсказку.',
    );
    auxiliary.append(caption);
    const details = htmlElement('details', 'chart-data-table chart-accessible-table');
    const summary = htmlElement('summary', '', 'Показать данные таблицей');
    const scroll = htmlElement('div', 'table-scroll');
    const table = htmlElement('table');
    table.id = `${state.id}-table`;
    const tableCaption = htmlElement('caption', '', contextText(chart));
    tableCaption.id = `${state.id}-table-caption`;
    table.append(tableCaption);
    const head = htmlElement('thead');
    const headerRow = htmlElement('tr');
    const firstHeader = htmlElement('th', '', 'Период / ответ');
    firstHeader.scope = 'col';
    headerRow.append(firstHeader);
    datasets.forEach((dataset, index) => {
      const header = htmlElement(
        'th',
        '',
        datasets.length > 1 ? `${index + 1}. ${dataset.label}` : dataset.label,
      );
      header.scope = 'col';
      headerRow.append(header);
    });
    head.append(headerRow);
    table.append(head);
    const body = htmlElement('tbody');
    labels.forEach((label, index) => {
      const row = htmlElement('tr');
      const header = htmlElement('th', '', label);
      header.scope = 'row';
      row.append(header);
      datasets.forEach((dataset) => {
        const value = dataset.values[index];
        row.append(
          htmlElement(
            'td',
            '',
            value === null || value === undefined
              ? 'Нет данных'
              : `${dataFormat.format(value)} ${chart.unit || ''}`.trim(),
          ),
        );
      });
      body.append(row);
    });
    table.append(body);
    scroll.append(table);
    details.append(summary, scroll);
    auxiliary.append(details);
    state.container.after(auxiliary);
    state.auxiliary = auxiliary;
  }

  function chooseTickIndices(positions, gap) {
    if (positions.length <= 1) return [0];
    const selected = [0, positions.length - 1];
    for (let index = 1; index < positions.length - 1; index += 1) {
      if (selected.every((other) => Math.abs(positions[index] - positions[other]) >= gap))
        selected.push(index);
    }
    return selected.sort((a, b) => a - b);
  }

  function renderLines(state, svg, width) {
    const { labels, datasets } = state;
    const allValues = datasets.flatMap((dataset) => dataset.values.filter(Number.isFinite));
    const min = Math.min(0, ...allValues);
    const max = Math.max(0, ...allValues);
    const span = max - min || 1;
    const numericWidth = Math.max(...[min, max].map((value) => formatAxis(value).length)) * 8;
    const left = Math.max(56, Math.min(150, numericWidth + 14));
    const right = datasets.length > 1 ? 42 : 20;
    const plotWidth = Math.max(80, width - left - right);
    const timeline = state.chart.x_values || [];
    const actualTime =
      timeline.length === labels.length &&
      timeline.length > 1 &&
      Math.max(...timeline) > Math.min(...timeline);
    const firstTime = actualTime ? Math.min(...timeline) : 0;
    const timeSpan = actualTime ? Math.max(...timeline) - firstTime : 1;
    const positions = labels.map(
      (_, index) =>
        left +
        (actualTime
          ? (timeline[index] - firstTime) / timeSpan
          : labels.length > 1
            ? index / (labels.length - 1)
            : 0.5) *
          plotWidth,
    );
    const tickIndices = chooseTickIndices(positions, width < 460 ? 110 : 125);
    const wrapped = tickIndices.map((index) =>
      splitLines(
        labels[index],
        Math.max(10, Math.floor(Math.min(120, plotWidth / Math.max(2, tickIndices.length)) / 7.5)),
      ),
    );
    const bottom = 28 + Math.max(...wrapped.map((lines) => lines.length)) * 18;
    const top = 22;
    const plotHeight = Math.max(220, datasets.length * 20 + 70);
    const height = top + plotHeight + bottom;
    const y = (value) => top + ((max - value) / span) * plotHeight;
    svg.setAttribute('height', String(height));
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    for (let index = 0; index <= 4; index += 1) {
      const value = min + (span * index) / 4;
      const yy = y(value);
      svg.append(
        svgElement('line', {
          x1: left,
          y1: yy,
          x2: left + plotWidth,
          y2: yy,
          stroke: themeColor('--chart-grid', '#d6dee8'),
          'stroke-dasharray': '3 4',
        }),
      );
      svg.append(
        svgElement(
          'text',
          {
            x: left - 10,
            y: yy + 5,
            'text-anchor': 'end',
            fill: themeColor('--chart-axis', '#526277'),
            'font-size': 14,
          },
          formatAxis(value),
        ),
      );
    }
    const endingLabels = [];
    datasets.forEach((dataset, seriesIndex) => {
      const color = seriesColor(seriesIndex);
      let segment = [];
      const flush = () => {
        if (segment.length > 1)
          svg.append(
            svgElement('polyline', {
              points: segment.join(' '),
              fill: 'none',
              stroke: color,
              'stroke-width': 3,
              'stroke-dasharray': dashPatterns[seriesIndex % dashPatterns.length],
              'data-series-index': seriesIndex,
            }),
          );
        segment = [];
      };
      dataset.values.forEach((value, index) => {
        if (Number.isFinite(value)) segment.push(`${positions[index]},${y(value)}`);
        else flush();
      });
      flush();
      let last;
      dataset.values.forEach((value, index) => {
        if (!Number.isFinite(value)) return;
        const point = marker(seriesIndex, positions[index], y(value), color);
        svg.append(point);
        inspectablePoint(state, point, labels[index], value, dataset.label);
        last = { x: positions[index], y: y(value) };
      });
      if (last && datasets.length > 1) endingLabels.push({ ...last, seriesIndex, color });
    });
    endingLabels.sort((a, b) => a.y - b.y);
    endingLabels.forEach((label, index) => {
      label.labelY = Math.max(
        top + 8,
        label.y,
        index ? endingLabels[index - 1].labelY + 18 : top + 8,
      );
    });
    if (endingLabels.length) {
      const excess = Math.max(0, endingLabels[endingLabels.length - 1].labelY - (top + plotHeight));
      endingLabels.forEach((label) => {
        label.labelY -= excess;
        const labelX = width - 16;
        svg.append(
          svgElement('line', {
            x1: label.x + 6,
            y1: label.y,
            x2: labelX - 8,
            y2: label.labelY,
            stroke: label.color,
            'stroke-width': 1.5,
          }),
        );
        svg.append(
          svgElement(
            'text',
            {
              x: labelX,
              y: label.labelY + 5,
              'text-anchor': 'middle',
              fill: label.color,
              'font-size': 14,
              'font-weight': 700,
            },
            String(label.seriesIndex + 1),
          ),
        );
      });
    }
    tickIndices.forEach((index, ordinal) => {
      multilineText(svg, wrapped[ordinal], positions[index], top + plotHeight + 25, {
        'text-anchor': index === 0 ? 'start' : index === labels.length - 1 ? 'end' : 'middle',
      });
    });
    return height;
  }

  function renderBars(state, svg, width) {
    const { labels, datasets } = state;
    const allValues = datasets.flatMap((dataset) => dataset.values.filter(Number.isFinite));
    const min = Math.min(0, ...allValues);
    const max = Math.max(0, ...allValues);
    const span = max - min || 1;
    const compact = width < 570;
    const textColor = themeColor('--chart-axis', '#526277');
    const longestValue = Math.max(...allValues.map((value) => dataFormat.format(value).length));
    const left = compact ? 24 : Math.min(290, Math.floor(width * 0.42));
    const right = compact
      ? 24
      : Math.min(Math.max(70, longestValue * 8 + 14), Math.max(80, width - left - 110));
    const plotWidth = Math.max(90, width - left - right - 12);
    const x = (value) => left + ((value - min) / span) * plotWidth;
    const zero = x(0);
    const rows = [];
    let cursor = 16;
    labels.forEach((label, index) => {
      const displayLabel =
        label === state.chart.title && (state.chart.period_summary || state.chart.period)
          ? state.chart.period_summary || state.chart.period
          : label;
      const lineWidth = compact ? width - 28 : left - 24;
      const lines = splitLines(displayLabel, Math.max(12, Math.floor(lineWidth / 7.4)));
      const barsHeight = datasets.length * (compact ? 44 : 27);
      const rowHeight = compact
        ? lines.length * 18 + barsHeight + 22
        : Math.max(lines.length * 18, barsHeight) + 25;
      rows.push({
        index,
        top: cursor,
        lines,
        height: rowHeight,
        barsTop: compact
          ? cursor + lines.length * 18 + 6
          : cursor + Math.max(0, (rowHeight - 25 - barsHeight) / 2),
      });
      cursor += rowHeight;
    });
    const height = Math.max(230, cursor + 45);
    svg.setAttribute('height', String(height));
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const tickCount = compact ? 1 : 4;
    for (let index = 0; index <= tickCount; index += 1) {
      const value = min + (span * index) / tickCount;
      const xx = x(value);
      svg.append(
        svgElement('line', {
          x1: xx,
          y1: 10,
          x2: xx,
          y2: cursor,
          stroke: themeColor('--chart-grid', '#d6dee8'),
          'stroke-dasharray': '3 4',
        }),
      );
      svg.append(
        svgElement(
          'text',
          {
            x: xx,
            y: height - 8,
            'text-anchor': index === 0 ? 'start' : index === tickCount ? 'end' : 'middle',
            fill: textColor,
            'font-size': 14,
          },
          formatAxis(value),
        ),
      );
    }
    rows.forEach((row) => {
      multilineText(svg, row.lines, compact ? 12 : left - 14, row.top + 14, {
        'text-anchor': compact ? 'start' : 'end',
      });
      datasets.forEach((dataset, seriesIndex) => {
        const value = dataset.values[row.index];
        if (!Number.isFinite(value)) return;
        const xx = x(value);
        const y = row.barsTop + seriesIndex * (compact ? 44 : 27);
        const color = seriesColor(seriesIndex);
        const bar = svgElement('rect', {
          x: Math.min(zero, xx),
          y,
          width: Math.max(2, Math.abs(xx - zero)),
          height: 18,
          rx: 2,
          fill: color,
          'data-series-index': seriesIndex,
        });
        svg.append(bar);
        inspectablePoint(state, bar, labels[row.index], value, dataset.label);
        const endMarker = marker(seriesIndex, xx, y + 9, color, 4);
        endMarker.setAttribute('aria-hidden', 'true');
        svg.append(endMarker);
        svg.append(
          svgElement(
            'text',
            {
              x: width - 8,
              y: y + (compact ? 36 : 14),
              'text-anchor': 'end',
              fill: textColor,
              'font-size': 14,
            },
            dataFormat.format(value),
          ),
        );
      });
    });
    return height;
  }

  function renderVisualization(state, force = false) {
    if (!state.container.isConnected) return;
    const width = Math.floor(
      state.frame.getBoundingClientRect().width || state.container.getBoundingClientRect().width,
    );
    if (width < 160 || (!force && width === state.width)) return;
    const hadFocus = state.container.contains(document.activeElement);
    state.width = width;
    state.points = [];
    hideTooltip(state);
    const svg = svgElement('svg', {
      width,
      role: 'img',
      'aria-labelledby': `${state.id}-title ${state.id}-description`,
      'aria-describedby': `${state.id}-table-caption`,
      focusable: 'false',
    });
    svg.append(svgElement('title', { id: `${state.id}-title` }, state.chart.title));
    svg.append(
      svgElement(
        'desc',
        { id: `${state.id}-description` },
        `${contextText(state.chart)}. Подробные значения приведены в таблице после диаграммы. Стрелки перемещают фокус между точками, Escape скрывает подсказку.`,
      ),
    );
    if (state.chart.kind === 'line') renderLines(state, svg, width);
    else renderBars(state, svg, width);
    state.frame.replaceChildren(svg);
    if (state.focusedPoint >= state.points.length) state.focusedPoint = 0;
    state.points.forEach((point, index) =>
      point.element.setAttribute('tabindex', index === state.focusedPoint ? '0' : '-1'),
    );
    if (hadFocus && state.points[state.focusedPoint])
      state.points[state.focusedPoint].element.focus({ preventScroll: true });
  }

  const chartObserver =
    typeof ResizeObserver === 'function'
      ? new ResizeObserver((entries) => {
          entries.forEach((entry) => {
            const state = chartStates.get(entry.target);
            if (!state) return;
            if (!state.container.isConnected) {
              chartObserver.unobserve(state.container);
              return;
            }
            cancelAnimationFrame(state.frameRequest);
            state.frameRequest = requestAnimationFrame(() => renderVisualization(state));
          });
        })
      : null;

  function disposeCharts(parent) {
    parent.querySelectorAll('.chart-container').forEach((container) => {
      const state = chartStates.get(container);
      if (!state) return;
      if (chartObserver) chartObserver.unobserve(container);
      cancelAnimationFrame(state.frameRequest);
      chartStates.delete(container);
    });
  }

  function drawChart(container, chart) {
    const prior = chartStates.get(container);
    if (prior) {
      prior.auxiliary.remove();
      if (chartObserver) chartObserver.unobserve(container);
      cancelAnimationFrame(prior.frameRequest);
    }
    const datasets = (
      chart.datasets && chart.datasets.length
        ? chart.datasets
        : [{ label: chart.title, values: chart.values || [] }]
    ).map((dataset) => ({ ...dataset, values: dataset.values.map(number) }));
    const state = {
      container,
      chart,
      labels: chart.labels || [],
      datasets,
      id: `chart-${++chartSequence}`,
      focusedPoint: 0,
      points: [],
      width: null,
    };
    container.replaceChildren();
    container.setAttribute('role', 'group');
    container.setAttribute('aria-label', contextText(chart));
    if (!datasets.some((dataset) => dataset.values.some(Number.isFinite))) {
      container.append(htmlElement('p', 'chart-empty', 'Нет числовых значений'));
      return;
    }
    state.frame = htmlElement('div', 'chart-frame');
    state.tooltip = htmlElement('div', 'chart-tooltip');
    state.tooltip.hidden = true;
    state.tooltip.id = `${state.id}-tooltip`;
    state.tooltip.setAttribute('role', 'tooltip');
    container.append(state.frame, state.tooltip);
    chartStates.set(container, state);
    buildAccessibleData(state);
    if (chartObserver) chartObserver.observe(container);
    state.frameRequest = requestAnimationFrame(() => renderVisualization(state));
  }

  function chartCard(chart) {
    const card = htmlElement('section', 'panel chart-panel');
    const heading = htmlElement('div', 'chart-heading');
    heading.append(
      htmlElement('h3', '', chart.title),
      htmlElement('span', 'unit-label', chart.unit),
    );
    card.append(
      heading,
      htmlElement(
        'p',
        'chart-context',
        `${chart.territory} · ${chart.period_summary || chart.period}`,
      ),
    );
    const container = htmlElement('div', 'chart-container');
    card.append(container);
    drawChart(container, chart);
    if (chart.notes) card.append(htmlElement('p', 'chart-note', chart.notes));
    const source = htmlElement('div', 'chart-source', 'Источник: ');
    const url = safeUrl(chart.source_url);
    if (url) {
      const link = htmlElement('a', '', chart.source_name || 'Исходный реестр');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      source.append(link);
    } else {
      source.append(document.createTextNode(chart.source_name || 'Исходный реестр'));
    }
    card.append(source);
    return card;
  }

  const initial = document.getElementById('chart-data');
  if (initial) {
    try {
      const charts = JSON.parse(initial.textContent);
      document.querySelectorAll('[data-chart-index]').forEach((container) => {
        const specification = charts[Number(container.dataset.chartIndex)];
        if (specification) drawChart(container, specification);
      });
    } catch (error) {
      console.error('Не удалось отобразить данные диаграмм.', error);
    }
  }

  const builder = document.getElementById('chart-builder');
  if (!builder) return;
  const urlState = new URL(location.href);
  urlState.searchParams.delete('page');
  const storageKey = `financial-chart-selection:${urlState.pathname}?${urlState.searchParams.toString()}`;
  let selected = new Set();
  try {
    selected = new Set(JSON.parse(sessionStorage.getItem(storageKey) || '[]').map(String));
  } catch (error) {
    selected = new Set();
  }
  const checks = [...document.querySelectorAll('.row-select')];
  const master = document.getElementById('select-page');
  const info = document.getElementById('chart-selection-summary');
  const output = document.getElementById('selected-charts');

  function syncSelection() {
    checks.forEach((checkbox) => {
      checkbox.checked = selected.has(checkbox.value);
    });
    if (master) {
      master.checked = checks.length > 0 && checks.every((checkbox) => checkbox.checked);
      master.indeterminate = checks.some((checkbox) => checkbox.checked) && !master.checked;
    }
    info.textContent = `Отмечено строк: ${selected.size}. Можно выбрать строки на нескольких страницах.`;
    try {
      sessionStorage.setItem(storageKey, JSON.stringify([...selected]));
    } catch (error) {
      // Browsers may disable session storage; the current page still works.
    }
  }
  checks.forEach((checkbox) => {
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selected.add(checkbox.value);
      else selected.delete(checkbox.value);
      syncSelection();
    });
  });
  if (master) {
    master.addEventListener('change', () => {
      checks.forEach((checkbox) => {
        if (master.checked) selected.add(checkbox.value);
        else selected.delete(checkbox.value);
      });
      syncSelection();
    });
  }
  document.getElementById('clear-selection').addEventListener('click', () => {
    selected.clear();
    syncSelection();
  });
  syncSelection();

  async function build(onlySelected) {
    if (onlySelected && !selected.size) {
      info.textContent = 'Отметьте строки в реестре для построения диаграммы.';
      return;
    }
    const endpoint = new URL(builder.dataset.chartEndpoint, location.origin);
    const parameters = new URLSearchParams(location.search);
    parameters.delete('page');
    // A section route applies a filter even when it is absent from the URL query.
    if (builder.dataset.chartSection) parameters.set('section', builder.dataset.chartSection);
    parameters.set('chart_kind', document.getElementById('chart-kind').value);
    if (onlySelected) parameters.set('ids', [...selected].join(','));
    endpoint.search = parameters.toString();
    const buttons = [
      document.getElementById('build-selection'),
      document.getElementById('build-filtered'),
    ];
    buttons.forEach((button) => {
      button.disabled = true;
    });
    info.textContent = 'Строим диаграммы…';
    try {
      const response = await fetch(endpoint, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error('Не удалось получить данные диаграммы. Обновите страницу.');
      const data = await response.json();
      disposeCharts(output);
      output.replaceChildren();
      let shown = 0;
      const more = htmlElement('button', 'button button-secondary', 'Показать ещё диаграммы');
      more.type = 'button';
      function append() {
        more.remove();
        const end = Math.min(shown + 6, data.charts.length);
        while (shown < end) output.append(chartCard(data.charts[shown++]));
        if (shown < data.charts.length) output.append(more);
        info.textContent = data.chart_count
          ? `Показано диаграмм: ${shown} из ${data.chart_count} · наблюдений в выборке: ${data.observations_count}.`
          : data.message;
      }
      more.addEventListener('click', append);
      append();
      const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      output.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
    } catch (error) {
      info.textContent = error.message;
    } finally {
      buttons.forEach((button) => {
        button.disabled = false;
      });
    }
  }
  document.getElementById('build-selection').addEventListener('click', () => build(true));
  document.getElementById('build-filtered').addEventListener('click', () => build(false));
})();
