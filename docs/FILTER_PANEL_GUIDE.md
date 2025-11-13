# Olympus Filter Panel - Modular Component

## Overview
The Olympus Filter Panel is a reusable component that provides consistent filtering functionality across multiple pages in the Olympus dashboard application.

## Components

### 1. HTML Partial
**Location:** `templates/dashboard/partials/_filter_panel.html`

The HTML template that renders the filter panel with:
- Period selection (Year/Quarter/Month)
- Investor filter
- Regional Manager filter
- Community filter
- Apply/Clear buttons

### 2. CSS Stylesheet
**Location:** `static/dashboard/filter_panel.css`

Styles for:
- Floating filter toggle button
- Filter pane sidebar
- Custom dropdowns with checkboxes
- Filter chips (selected filters)
- Loading overlay
- Responsive behavior for drawer/filter combinations

### 3. JavaScript Module
**Location:** `static/dashboard/filter_panel.js`

Provides:
- Filter pane toggle functionality
- Custom dropdown with search and multi-select
- Filter chip rendering and removal
- Period mode switching (Year/Quarter/Month)
- Apply/Clear filter actions
- URL parameter management

## Usage

### Step 1: Include CSS in Template
```django
{% load static %}

{% block extra_head %}
<link rel="stylesheet" href="{% static 'dashboard/filter_panel.css' %}">
<!-- Your page-specific styles -->
<style>
    /* ... */
</style>
{% endblock %}
```

### Step 2: Include HTML Partial
```django
{% block content %}
<!-- Loading overlay -->
<div id="loadingOverlay" class="loading-overlay" role="status" aria-hidden="true">
    <div class="loading-box">
        <div class="loading-spinner"></div>
        <div class="loading-text">Loading...</div>
    </div>
</div>

<!-- Include Filter Panel -->
{% include 'dashboard/partials/_filter_panel.html' with filter_action_url='/your-page-url/' %}

<!-- Your page content -->
<div class="your-container">
    <!-- ... -->
</div>
{% endblock %}
```

### Step 3: Include JavaScript
```django
{% block extra_scripts %}
<!-- Set the filter action URL -->
<script>
    window.filterActionUrl = '/your-page-url/';
</script>
<script src="{% static 'dashboard/filter_panel.js' %}"></script>

<!-- Your page-specific scripts -->
<script>
    // ... your custom code ...
</script>
{% endblock %}
```

### Step 4: Prepare Context Data in View
```python
from dashboard.dashboard_service import DashboardPageService

def your_view(request):
    service = DashboardPageService(request)
    context = service.get_dashboard_context()  # Returns period_options, investors, etc.
    
    # Add your page-specific data
    context['your_data'] = ...
    
    return render(request, 'your_template.html', context)
```

## Configuration

### Filter Action URL
Set the URL where filters will redirect:
```javascript
window.filterActionUrl = '/financial-reporting/';  // or '/dashboard/', etc.
```

### Available Context Variables
The filter panel expects these context variables from your view:
- `period_options`: Dict of years with quarters/months
- `investors`: List of investor names
- `regional_managers`: List of regional manager names
- `communities`: List of community objects
- `selected_period`: Currently selected period (optional)
- `selected_year`: Currently selected year (optional)
- `selected_investor`: List of selected investors (optional)
- `selected_regional_manager`: List of selected regional managers (optional)
- `selected_community`: List of selected communities (optional)

## Features

### Multi-Select Dropdowns
- Click items to select/deselect
- Search functionality
- Select All / Clear All buttons
- Visual chips showing selections

### Period Modes
- **Year**: Select one or multiple years
- **Quarter**: Select specific quarters
- **Month**: Select specific months

### URL Parameters
The filter panel automatically manages these URL parameters:
- `period_mode`: year/quarter/month
- `period_year`: Selected year(s)
- `period_quarter`: Selected quarter(s)
- `period_month`: Selected month(s)
- `investor`: Selected investor(s)
- `regional_manager`: Selected regional manager(s)
- `community`: Selected community/communities

### Responsive Design
- Adjusts layout when navigation drawer is open
- Reduces KPI card sizes to prevent wrapping
- Proper z-index layering
- Mobile-friendly

## API

### Global Functions
The filter panel exposes these functions via `window.OlympusFilterPanel`:

```javascript
// Set filter mode (year/quarter/month)
window.OlympusFilterPanel.setMode('month');

// Programmatically apply filters
window.OlympusFilterPanel.applyFilters();

// Clear all filters
window.OlympusFilterPanel.clearFilters();

// Re-render chips for a select element
window.OlympusFilterPanel.renderChips(selectElement, 'chipsContainerId');
```

## Examples

### Dashboard Page
```django
{% include 'dashboard/partials/_filter_panel.html' with filter_action_url='/dashboard/' %}
```

### Financial Reporting Page
```django
{% include 'dashboard/partials/_filter_panel.html' with filter_action_url='/financial-reporting/' %}
```

## Customization

### Adding New Filter Categories
1. Add filter section to `_filter_panel.html`:
```django
<div class="filter-section">
    <div style="font-weight:700;margin-bottom:8px;color:#0B3A3F">New Filter</div>
    <div id="newFilterDropdownContainer" class="custom-dropdown">
        <!-- dropdown markup -->
    </div>
</div>
```

2. Initialize in `filter_panel.js`:
```javascript
const newFilterDropdown = initCustomDropdown(
    'newFilterDropdownToggle',
    'newFilterDropdownMenu',
    'filterNewFilter',
    'newFilterChips',
    'newFilterSearchInput'
);
```

3. Add to apply/clear functions to handle the new filter parameter.

## Best Practices

1. **Always set `window.filterActionUrl`** before including the script
2. **Use DashboardPageService** to get consistent context data
3. **Keep page-specific scripts separate** from filter panel logic
4. **Test with both drawer open and closed** to ensure proper layout
5. **Preserve loading overlay** for smooth transitions

## Troubleshooting

### Filters Not Working
- Check `window.filterActionUrl` is set correctly
- Verify context variables are passed from view
- Check browser console for JavaScript errors

### Layout Issues
- Ensure filter_panel.css is loaded before page-specific styles
- Check for CSS conflicts with custom styles
- Verify `.main` container exists for proper spacing

### Dropdown Not Opening
- Ensure all IDs match between HTML and JavaScript
- Check for JavaScript errors in console
- Verify dropdown initialization in filter_panel.js

## Future Enhancements
- Add filter presets/saved filters
- Export/import filter configurations
- Filter history and recent filters
- Advanced filter combinations (AND/OR logic)
- Date range pickers for custom periods
