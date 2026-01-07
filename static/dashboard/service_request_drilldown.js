/**
 * Service Request Drill-Down Chart Handler
 * Manages interactive drill-down/up functionality for category and move-in charts
 */

class ServiceRequestDrillHandler {
  constructor(categoryChartId, moveInChartId, chartPayload) {
    this.categoryChartId = categoryChartId;
    this.moveInChartId = moveInChartId;
    this.originalPayload = JSON.parse(JSON.stringify(chartPayload)); // Deep clone
    this.currentPayload = chartPayload;
    this.drillState = {
      isDrilled: false,
      category: null,
      level: 'category' // 'category' or 'item'
    };
    this.categoryChart = null;
    this.moveInChart = null;
    this.drillUpButton = null;
    this.palette = ['#2563eb','#1d4ed8','#1e40af','#6d28d9','#7c3aed','#a855f7','#9333ea','#f97316','#ea580c','#fb923c','#0ea5e9','#14b8a6'];
  }

  formatCount(value) {
    var v = Number(value) || 0;
    var abs = Math.abs(v);
    if(abs >= 1000000) return (v / 1000000).toFixed(1) + 'M';
    if(abs >= 1000) return (v / 1000).toFixed(1) + 'K';
    return v.toLocaleString();
  }

  createDrillUpButton() {
    // Create drill-up button container
    var buttonContainer = document.createElement('div');
    buttonContainer.id = 'drillUpContainer';
    buttonContainer.style.cssText = 'margin: 15px 0; padding: 10px; background: #f0f9ff; border-left: 4px solid #0ea5e9; display: none;';
    
    var button = document.createElement('button');
    button.id = 'drillUpButton';
    button.innerHTML = '⬆ Drill Up to All Categories';
    button.style.cssText = 'padding: 8px 16px; background: #0ea5e9; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: 500;';
    button.onclick = () => this.drillUp();
    
    var breadcrumb = document.createElement('span');
    breadcrumb.id = 'drillBreadcrumb';
    breadcrumb.style.cssText = 'margin-left: 15px; color: #0369a1; font-weight: 500;';
    
    buttonContainer.appendChild(button);
    buttonContainer.appendChild(breadcrumb);
    
    // Insert before charts section
    var chartsSection = document.querySelector('.charts-grid');
    if (chartsSection) {
      chartsSection.parentNode.insertBefore(buttonContainer, chartsSection);
    }
    
    this.drillUpButton = buttonContainer;
  }

  renderCategoryChart(canvas, data) {
    if (this.categoryChart) {
      this.categoryChart.destroy();
    }
    
    var self = this;
    this.categoryChart = new Chart(canvas, {
      type: 'doughnut',
      data: {
        labels: data.labels,
        datasets: [{
          data: (data.values || []).map(function(v){ return Number(v) || 0; }),
          backgroundColor: this.palette.slice(0, data.labels.length),
          borderColor: '#ffffff',
          borderWidth: 2
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (event, elements) => {
          if (elements.length > 0 && !this.drillState.isDrilled) {
            var index = elements[0].index;
            var category = data.labels[index];
            this.drillDown(category);
          }
        },
        plugins: {
          legend: { position: 'right' },
          tooltip: {
            callbacks: {
              label: function(context) {
                var label = context.label || '';
                var value = Number(context.parsed || 0);
                var total = context.dataset.data.reduce(function(sum, current){ 
                  return sum + Number(current || 0); 
                }, 0);
                var pct = total ? (value / total * 100) : 0;
                return label + ': ' + self.formatCount(value) + ' (' + pct.toFixed(2) + '%)';
              },
              afterLabel: function(context) {
                if (!self.drillState.isDrilled) {
                  return '↓ Click to drill down';
                }
                return '';
              }
            }
          }
        }
      }
    });
  }

  renderMoveInChart(canvas, data) {
    if (this.moveInChart) {
      this.moveInChart.destroy();
    }
    
    var self = this;
    this.moveInChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: data.labels,
        datasets: [{
          label: 'Requests',
          data: (data.values || []).map(function(v){ return Number(v) || 0; }),
          backgroundColor: '#0ea5e9'
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        onClick: (event, elements) => {
          if (elements.length > 0 && !this.drillState.isDrilled) {
            var index = elements[0].index;
            var category = data.labels[index];
            this.drillDown(category);
          }
        },
        scales: {
          x: {
            beginAtZero: true,
            ticks: {
              callback: function(value) {
                return self.formatCount(value);
              }
            }
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function(context) {
                var value = Number(context.parsed.x || 0);
                return context.label + ': ' + self.formatCount(value);
              },
              afterLabel: function(context) {
                if (!self.drillState.isDrilled) {
                  return '↓ Click to drill down';
                }
                return '';
              }
            }
          }
        }
      }
    });
  }

  drillDown(category) {
    console.log('Drilling down to category:', category);
    
    // Instead of AJAX + reload, just reload with drill_category parameter
    // The initialize() method will detect it and auto-drill with server-filtered data
    var currentUrl = new URL(window.location.href);
    var currentDrillCategory = currentUrl.searchParams.get('drill_category');
    
    // Only reload if not already on this category
    if (currentDrillCategory !== category) {
      currentUrl.searchParams.set('drill_category', category);
      currentUrl.searchParams.set('page', '1'); // Reset to first page
      window.location.href = currentUrl.toString();
    }
  }

  drillUp() {
    console.log('Drilling up to all categories');
    
    // Simply navigate to URL without drill_category - don't pre-render anything
    // The server will render the correct state on page load
    this.clearTableFilter();
  }

  clearTableFilter() {
    // Navigate to URL without drill_category parameter
    var currentUrl = new URL(window.location.href);
    currentUrl.searchParams.delete('drill_category');
    currentUrl.searchParams.set('page', '1'); // Reset to first page
    
    // Navigate to new URL (this will cause a page load with clean state)
    window.location.href = currentUrl.toString();
  }

  initialize() {
    this.createDrillUpButton();
    
    var categoryCanvas = document.getElementById(this.categoryChartId);
    var moveInCanvas = document.getElementById(this.moveInChartId);
    
    // Check if we should start in drilled-down state
    var urlParams = new URLSearchParams(window.location.search);
    var drillCategory = urlParams.get('drill_category');
    
    if (drillCategory) {
      // Fetch and display drilled data from server
      this.drillState.isDrilled = true;
      this.drillState.category = drillCategory;
      
      // Show drill-up button
      if (this.drillUpButton) {
        this.drillUpButton.style.display = 'block';
        var breadcrumb = document.getElementById('drillBreadcrumb');
        if (breadcrumb) {
          breadcrumb.textContent = 'Viewing: ' + drillCategory + ' → Items';
        }
      }
      
      // Fetch drilled data
      fetch('/drillthrough/service-request/drill-data/?' + urlParams.toString())
        .then(response => response.json())
        .then(data => {
          if (categoryCanvas && data.category_breakdown) {
            this.renderCategoryChart(categoryCanvas, data.category_breakdown);
          }
          if (moveInCanvas && data.move_in_within_five_days) {
            this.renderMoveInChart(moveInCanvas, data.move_in_within_five_days);
          }
        })
        .catch(error => {
          console.error('Failed to load drill data:', error);
          // Fallback to category-level charts
          if (categoryCanvas && this.currentPayload.category_breakdown) {
            this.renderCategoryChart(categoryCanvas, this.currentPayload.category_breakdown);
          }
          if (moveInCanvas && this.currentPayload.move_in_within_five_days) {
            this.renderMoveInChart(moveInCanvas, this.currentPayload.move_in_within_five_days);
          }
        });
    } else {
      // Show category-level charts
      if (categoryCanvas && this.currentPayload.category_breakdown) {
        this.renderCategoryChart(categoryCanvas, this.currentPayload.category_breakdown);
      }
      
      if (moveInCanvas && this.currentPayload.move_in_within_five_days) {
        this.renderMoveInChart(moveInCanvas, this.currentPayload.move_in_within_five_days);
      }
    }
  }
}
