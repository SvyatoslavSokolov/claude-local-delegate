let charts = {};
function renderCharts(runs) {
  if (!runs || runs.length === 0) return;

  const daily = {};
  runs.forEach(r => {
    if (!r.at_str || r.at_str === '-') return;
    const date = r.at_str.split(' ')[0];
    if (!daily[date]) {
      daily[date] = { count: 0, inTokens: 0, cacheTokens: 0, outTokens: 0, cost: 0, duration: 0 };
    }
    daily[date].count += 1;
    const cache = r.cached_input_tokens || 0;
    const totalIn = r.total_input_tokens || 0;
    daily[date].inTokens += (totalIn - cache);
    daily[date].cacheTokens += cache;
    daily[date].outTokens += (r.output_tokens || 0);
    daily[date].cost += (r.local_cost || 0);
    daily[date].duration += (r.duration_s || 0);
  });

  const sortedDates = Object.keys(daily).sort();
  if (sortedDates.length === 0) return;

  const dataRuns = sortedDates.map(d => daily[d].count);
  const dataIn = sortedDates.map(d => daily[d].inTokens);
  const dataCache = sortedDates.map(d => daily[d].cacheTokens);
  const dataOut = sortedDates.map(d => daily[d].outTokens);
  const dataCost = sortedDates.map(d => daily[d].cost);
  const dataDur = sortedDates.map(d => daily[d].count ? (daily[d].duration / daily[d].count) : 0);

  const createChart = (id, type, data, options = {}) => {
    const ctx = document.getElementById(id);
    if (!ctx) return;
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(ctx, {
      type, data,
      options: {
        responsive: true,
        plugins: {
          legend: { labels: { color: '#c9d1d9' } }
        },
        scales: {
          x: { ticks: { color: '#8b949e' }, grid: { color: '#30363d' } },
          y: { ticks: { color: '#8b949e' }, grid: { color: '#30363d' }, beginAtZero: true }
        },
        ...options
      }
    });
  };

  createChart('runsChart', 'bar', {
    labels: sortedDates,
    datasets: [{
      label: 'Tasks Count',
      data: dataRuns,
      backgroundColor: '#58a6ff'
    }]
  });

  createChart('tokensChart', 'bar', {
    labels: sortedDates,
    datasets: [
      { label: 'Out Tokens', data: dataOut, backgroundColor: '#3fb950' },
      { label: 'Cache Tokens', data: dataCache, backgroundColor: '#bc8cff' },
      { label: 'In Tokens', data: dataIn, backgroundColor: '#f85149' }
    ]
  }, {
    scales: {
      x: { stacked: true, ticks: { color: '#8b949e' }, grid: { color: '#30363d' } },
      y: { stacked: true, ticks: { color: '#8b949e' }, grid: { color: '#30363d' }, beginAtZero: true }
    }
  });

  createChart('costChart', 'line', {
    labels: sortedDates,
    datasets: [{
      label: 'Local Cost ($)',
      data: dataCost,
      borderColor: '#d29922',
      backgroundColor: 'rgba(210, 153, 34, 0.2)',
      fill: true,
      tension: 0.3
    }]
  });

  createChart('durationChart', 'line', {
    labels: sortedDates,
    datasets: [{
      label: 'Avg Duration (s)',
      data: dataDur,
      borderColor: '#8b949e',
      backgroundColor: 'rgba(139, 148, 158, 0.2)',
      fill: true,
      tension: 0.3
    }]
  });
}
