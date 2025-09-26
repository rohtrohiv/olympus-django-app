from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Sum, Avg, Count, Q, F
from .models import OlympusLeaseTrendAnalysis
from datetime import datetime
import json
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.template.loader import render_to_string
import re


def sample_page(request):
	"""Simple page to test the shared base layout."""
	return render(request, 'sample.html', { 'page_title': 'Sample — Layout Test' })


@login_required
def property_analytics(request):
    """Simple property analytics placeholder page.

    This page will be expanded later with charts and filters. For now it
    renders a template that extends `base.html` so the topbar/drawer behavior
    can be tested and the page is reachable from the header links.
    """
    context = {
        'page_title': 'Property Analytics',
        # provide small example stats to render in the template
        'summary': {
            'properties_monitored': 124,
            'active_leases': 987,
            'avg_occupancy': '92.4%'
        }
    }
    return render(request, 'property_analytics.html', context)


@login_required
def advanced_analytics(request):
    """Advanced analytics page with comprehensive data analysis tools.
    
    Provides filtering, grouping, charting, and export capabilities for 
    property data analysis using all available columns from the model.
    """
    # Load real filter options from the database
    try:
        properties = list(OlympusLeaseTrendAnalysis.objects.values_list('property_name', flat=True).distinct().order_by('property_name'))
        investors = list(OlympusLeaseTrendAnalysis.objects.values_list('investor', flat=True).distinct().order_by('investor'))
        managers = list(OlympusLeaseTrendAnalysis.objects.values_list('regional_area_manager', flat=True).distinct().order_by('regional_area_manager'))
        
        # Remove empty/null values
        properties = [p for p in properties if p and str(p).strip()]
        investors = [i for i in investors if i and str(i).strip()]
        managers = [m for m in managers if m and str(m).strip()]
        
        # Create cascading filter mappings
        # investor -> properties mapping
        investor_properties = {}
        for investor in investors:
            props = list(OlympusLeaseTrendAnalysis.objects.filter(investor=investor).values_list('property_name', flat=True).distinct())
            investor_properties[investor] = [p for p in props if p and str(p).strip()]
        
        # investor -> managers mapping  
        investor_managers = {}
        for investor in investors:
            mgrs = list(OlympusLeaseTrendAnalysis.objects.filter(investor=investor).values_list('regional_area_manager', flat=True).distinct())
            investor_managers[investor] = [m for m in mgrs if m and str(m).strip()]
        
        # property -> managers mapping (for additional cascading)
        property_managers = {}
        for prop in properties:
            mgrs = list(OlympusLeaseTrendAnalysis.objects.filter(property_name=prop).values_list('regional_area_manager', flat=True).distinct())
            property_managers[prop] = [m for m in mgrs if m and str(m).strip()]
        
    except Exception as e:
        # Fallback to empty lists if database query fails
        properties = []
        investors = []  
        managers = []
        investor_properties = {}
        investor_managers = {}
        property_managers = {}
    
    # Serialize to JSON for JavaScript
    import json
    context = {
        'page_title': 'Advanced Property Analytics',
        'properties': properties,
        'investors': investors,
        'managers': managers,
        'investor_properties_json': json.dumps(investor_properties),
        'investor_managers_json': json.dumps(investor_managers),
        'property_managers_json': json.dumps(property_managers),
        'properties_json': json.dumps(properties),
        'managers_json': json.dumps(managers),
    }
    return render(request, 'property_analytics_advanced.html', context)


@login_required 
def analytics_query(request):
    """API endpoint for advanced analytics queries.
    
    Processes filter parameters and returns aggregated data for charts,
    tables, and summary statistics. Supports various groupings and metrics.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)
    
    try:
        # Get filter parameters
        date_range = request.POST.get('date_range', 'last_30_days')
        start_date = request.POST.get('start_date')
        end_date = request.POST.get('end_date') 
        properties = request.POST.getlist('properties')
        investors = request.POST.getlist('investors')
        managers = request.POST.getlist('managers')
        group_by = request.POST.get('group_by', '')
        metrics = request.POST.getlist('metrics')
        aggregation = request.POST.get('aggregation', 'sum')
        
        # Build base queryset
        qs = OlympusLeaseTrendAnalysis.objects.all()
        
        # Apply date filters
        from datetime import datetime, timedelta
        if date_range == 'custom' and start_date and end_date:
            # Handle both date formats: YYYY-MM-DD and Mon-YYYY
            qs = qs.filter(
                Q(snapshotdate__gte=start_date, snapshotdate__lte=end_date) |
                Q(snapshotdate__contains=start_date[:4]) |
                Q(snapshotdate__contains=end_date[:4])
            )
        elif date_range == 'last_30_days':
            thirty_days_ago = datetime.now() - timedelta(days=30)
            cutoff_date = thirty_days_ago.strftime('%Y-%m-%d')
            cutoff_month = thirty_days_ago.strftime('%b-%Y')
            qs = qs.filter(
                Q(snapshotdate__gte=cutoff_date) |
                Q(snapshotdate__contains=cutoff_month)
            )
        elif date_range == 'last_90_days':
            ninety_days_ago = datetime.now() - timedelta(days=90)
            cutoff_date = ninety_days_ago.strftime('%Y-%m-%d')
            cutoff_year = ninety_days_ago.strftime('%Y')
            qs = qs.filter(
                Q(snapshotdate__gte=cutoff_date) |
                Q(snapshotdate__contains=cutoff_year)
            )
        elif date_range == 'last_6_months':
            six_months_ago = datetime.now() - timedelta(days=180)
            cutoff_year = six_months_ago.strftime('%Y')
            qs = qs.filter(snapshotdate__contains=cutoff_year)
        elif date_range == 'last_year':
            one_year_ago = datetime.now() - timedelta(days=365)
            cutoff_year = one_year_ago.strftime('%Y')
            current_year = datetime.now().strftime('%Y')
            qs = qs.filter(
                Q(snapshotdate__contains=cutoff_year) |
                Q(snapshotdate__contains=current_year)
            )
        
        # Apply other filters
        if properties and 'all' not in properties:
            qs = qs.filter(property_name__in=properties)
        if investors and 'all' not in investors:
            qs = qs.filter(investor__in=investors) 
        if managers and 'all' not in managers:
            qs = qs.filter(regional_area_manager__in=managers)
            
        # Prepare response data structure
        response_data = {
            'summary': {},
            'chart_data': {},
            'table_data': [],
            'insights': []
        }
        
        # Helper function to safely convert to numeric
        def safe_float(value, default=0.0):
            try:
                if value in ['', None, 'None', 'null']:
                    return default
                return float(str(value).replace('$', '').replace(',', '').replace('%', ''))
            except (ValueError, TypeError):
                return default
        
        def safe_int(value, default=0):
            try:
                if value in ['', None, 'None', 'null']:
                    return default
                return int(float(str(value).replace('$', '').replace(',', '').replace('%', '')))
            except (ValueError, TypeError):
                return default
        
        # Calculate summary statistics
        if qs.exists():
            # Get all data and calculate stats manually due to mixed data types
            all_data = list(qs.values())
            
            total_properties = len(set(item['property_number'] for item in all_data))
            total_units = sum(safe_int(item['total_units']) for item in all_data if item['total_units'])
            occupied_units = sum(safe_int(item['occupied_units']) for item in all_data if item['occupied_units'])
            
            # Calculate occupancy rates
            occupancy_rates = [safe_float(item['percentage_occupacy']) for item in all_data if item['percentage_occupacy']]
            avg_occupancy = sum(occupancy_rates) / len(occupancy_rates) if occupancy_rates else 0
            
            # Move ins/outs
            move_ins = sum(safe_int(item['total_move_ins']) for item in all_data if item['total_move_ins'])
            move_outs = sum(safe_int(item['total_move_outs']) for item in all_data if item['total_move_outs'])
            
            # Applications and other metrics
            applications = sum(safe_int(item['applications']) for item in all_data if item['applications'])
            vacant_units = sum(safe_int(item['vacant_units_without_down_admin']) for item in all_data if item['vacant_units_without_down_admin'])
            
            response_data['summary'] = {
                'totalProperties': total_properties,
                'totalUnits': total_units,
                'occupiedUnits': occupied_units,
                'avgOccupancy': round(avg_occupancy, 1),
                'totalMoveIns': move_ins,
                'totalMoveOuts': move_outs,
                'totalApplications': applications,
                'vacantUnits': vacant_units
            }
            
            # Generate insights
            insights = []
            if avg_occupancy > 0:
                if avg_occupancy > 95:
                    insights.append(f"Exceptional occupancy rate of {avg_occupancy:.1f}% - consider rent optimization")
                elif avg_occupancy > 90:
                    insights.append(f"Excellent occupancy performance at {avg_occupancy:.1f}%")
                elif avg_occupancy > 85:
                    insights.append(f"Good occupancy levels at {avg_occupancy:.1f}%")
                else:
                    insights.append(f"Occupancy at {avg_occupancy:.1f}% - improvement opportunities available")
            
            if move_ins > 0 and move_outs > 0:
                net_change = move_ins - move_outs
                if net_change > 0:
                    insights.append(f"Strong leasing activity: +{net_change} net move-ins")
                elif net_change < 0:
                    insights.append(f"Higher turnover: {net_change} net move-outs")
                else:
                    insights.append("Balanced tenant turnover activity")
            
            if applications > 0:
                insights.append(f"Strong demand with {applications:,} applications received")
            
            if vacant_units > 0:
                insights.append(f"{vacant_units:,} vacant units available for lease-up")
                    
            response_data['insights'] = insights
            
            # Generate table data with proper field mapping
            table_fields = {
                'property_name': 'Property Name',
                'investor': 'Investor', 
                'regional_area_manager': 'Regional Manager',
                'regional_director': 'Regional Director',
                'total_units': 'Total Units',
                'occupied_units': 'Occupied Units', 
                'percentage_occupacy': 'Occupancy %',
                'total_move_ins': 'Move Ins',
                'total_move_outs': 'Move Outs',
                'applications': 'Applications',
                'effective_rent': 'Effective Rent',
                'market_rent': 'Market Rent',
                'vacant_units_without_down_admin': 'Vacant Units'
            }
            
            # Build field list based on selected metrics or default
            selected_fields = ['property_name', 'investor', 'regional_area_manager']
            if metrics:
                selected_fields.extend([field for field in table_fields.keys() if field in metrics])
            else:
                selected_fields.extend(['total_units', 'occupied_units', 'percentage_occupacy'])
            
            # Remove duplicates while preserving order
            seen = set()
            selected_fields = [x for x in selected_fields if not (x in seen or seen.add(x))]
            
            # If grouping, put group field first
            if group_by and group_by in table_fields:
                if group_by in selected_fields:
                    selected_fields.remove(group_by)
                selected_fields.insert(0, group_by)
            
            table_data = []
            for item in all_data[:100]:  # Limit to 100 rows for performance
                table_row = {}
                for field in selected_fields:
                    if field in table_fields:
                        display_name = table_fields[field]
                        raw_value = item[field]
                        
                        # Format values based on field type
                        if field in ['total_units', 'occupied_units', 'total_move_ins', 'total_move_outs', 'applications', 'vacant_units_without_down_admin']:
                            table_row[display_name] = safe_int(raw_value)
                        elif field == 'percentage_occupacy':
                            table_row[display_name] = f"{safe_float(raw_value):.1f}%"
                        elif field in ['effective_rent', 'market_rent', 'avg_amount_per_sqft']:
                            table_row[display_name] = f"${safe_float(raw_value):,.2f}" if raw_value else "-"
                        else:
                            table_row[display_name] = str(raw_value) if raw_value else "-"
                
                table_data.append(table_row)
            
            response_data['table_data'] = table_data
            
            # Generate chart data
            if group_by and group_by in table_fields:
                # Group data for chart
                from collections import defaultdict
                grouped_data = defaultdict(list)
                
                for item in all_data:
                    group_value = str(item[group_by])
                    if group_value:
                        grouped_data[group_value].append(item)
                
                chart_labels = []
                datasets = []
                
                # Use selected metrics or default to occupancy
                selected_metrics = metrics if metrics else ['percentage_occupacy']
                
                # Prepare chart labels (groups)
                group_names = list(grouped_data.keys())[:10]  # Limit to 10 groups
                chart_labels = group_names
                
                # Create a dataset for each selected metric
                colors = ['#59E6F6', '#F59E0B', '#10B981', '#EF4444', '#8B5CF6', '#F97316']
                
                for i, metric in enumerate(selected_metrics[:6]):  # Limit to 6 metrics for readability
                    metric_label = table_fields.get(metric, metric.replace('_', ' ').title())
                    chart_values = []
                    
                    for group_name in group_names:
                        group_items = grouped_data[group_name]
                        
                        # Get values for the selected metric
                        if metric == 'percentage_occupacy':
                            metric_values = [safe_float(item[metric]) for item in group_items if item[metric]]
                        else:
                            metric_values = [safe_float(item[metric]) if metric in ['avg_amount_per_sqft', 'effective_rent', 'market_rent'] 
                                           else safe_int(item[metric]) for item in group_items if item[metric]]
                        
                        # Apply aggregation
                        if metric_values:
                            if aggregation == 'sum':
                                agg_value = sum(metric_values)
                            elif aggregation == 'avg':
                                agg_value = sum(metric_values) / len(metric_values)
                            elif aggregation == 'max':
                                agg_value = max(metric_values)
                            elif aggregation == 'min':
                                agg_value = min(metric_values)
                            elif aggregation == 'count':
                                agg_value = len(metric_values)
                            else:
                                agg_value = sum(metric_values)
                            
                            chart_values.append(round(agg_value, 2))
                        else:
                            chart_values.append(0)
                    
                    aggregation_label = aggregation.capitalize()
                    dataset_label = f'{aggregation_label} {metric_label}'
                    
                    color = colors[i % len(colors)]
                    # Create background color with opacity
                    if color == '#59E6F6':
                        bg_color = 'rgba(89, 230, 246, 0.1)'
                    elif color == '#F59E0B':
                        bg_color = 'rgba(245, 158, 11, 0.1)'
                    elif color == '#10B981':
                        bg_color = 'rgba(16, 185, 129, 0.1)'
                    elif color == '#EF4444':
                        bg_color = 'rgba(239, 68, 68, 0.1)'
                    elif color == '#8B5CF6':
                        bg_color = 'rgba(139, 92, 246, 0.1)'
                    elif color == '#F97316':
                        bg_color = 'rgba(249, 115, 22, 0.1)'
                    else:
                        bg_color = 'rgba(89, 230, 246, 0.1)'
                    
                    datasets.append({
                        'label': dataset_label,
                        'data': chart_values,
                        'borderColor': color,
                        'backgroundColor': bg_color,
                        'tension': 0.4
                    })
                
                response_data['chart_data'] = {
                    'labels': chart_labels,
                    'datasets': datasets
                }
            else:
                # Time-series chart by snapshot date
                from collections import defaultdict
                from datetime import datetime, timedelta
                import calendar
                
                date_data = defaultdict(lambda: defaultdict(list))
                
                # Use selected metrics or default to occupancy
                selected_metrics = metrics if metrics else ['percentage_occupacy']
                
                # Organize data by date and metric
                for item in all_data:
                    date_key = item['snapshotdate']
                    if date_key:
                        for metric in selected_metrics:
                            if metric == 'percentage_occupacy':
                                value = safe_float(item[metric])
                            else:
                                value = safe_float(item[metric]) if metric in ['avg_amount_per_sqft', 'effective_rent', 'market_rent'] else safe_int(item[metric])
                            date_data[date_key][metric].append(value)
                
                chart_labels = []
                datasets = []
                
                # Enhanced date handling based on date range
                if date_range == 'last_30_days':
                    # For last 30 days, show individual dates
                    sorted_dates = sorted(date_data.keys())
                    
                    # Generate labels for all dates in the last 30 days
                    today = datetime.now()
                    date_range_labels = []
                    date_range_keys = []
                    
                    for i in range(30):
                        check_date = today - timedelta(days=i)
                        date_key = check_date.strftime('%Y-%m-%d')
                        date_label = check_date.strftime('%m/%d')
                        date_range_keys.insert(0, date_key)  # Insert at beginning to maintain chronological order
                        date_range_labels.insert(0, date_label)
                    
                    # Also check for month-year format dates
                    month_year_dates = []
                    for date_key in sorted_dates:
                        if len(date_key) <= 8 and '-' in date_key:  # Mon-YYYY format
                            try:
                                # Convert Mon-YYYY to datetime
                                month_name, year = date_key.split('-')
                                month_num = list(calendar.month_abbr).index(month_name)
                                # Use last day of month for month-year dates
                                last_day = calendar.monthrange(int(year), month_num)[1]
                                converted_date = f"{year}-{month_num:02d}-{last_day:02d}"
                                month_year_dates.append((converted_date, date_key))
                            except (ValueError, IndexError):
                                pass
                    
                    # Include month-year dates in the range if they fall within last 30 days
                    for converted_date, original_key in month_year_dates:
                        try:
                            date_obj = datetime.strptime(converted_date, '%Y-%m-%d')
                            if (today - date_obj).days <= 30:
                                date_label = date_obj.strftime('%m/%d')
                                if converted_date not in date_range_keys:
                                    # Find correct position to insert
                                    insert_pos = 0
                                    for i, existing_key in enumerate(date_range_keys):
                                        if existing_key < converted_date:
                                            insert_pos = i + 1
                                        else:
                                            break
                                    date_range_keys.insert(insert_pos, converted_date)
                                    date_range_labels.insert(insert_pos, date_label)
                                    # Map original key to converted key for data lookup
                                    if original_key in date_data:
                                        date_data[converted_date] = date_data[original_key]
                        except ValueError:
                            pass
                    
                    chart_labels = date_range_labels
                    final_date_keys = date_range_keys
                    
                else:
                    # For other date ranges, group by month and show month-end data
                    monthly_data = defaultdict(lambda: defaultdict(list))
                    
                    for date_key in date_data.keys():
                        try:
                            # Handle different date formats
                            if len(date_key) > 8 and '-' in date_key:  # YYYY-MM-DD format
                                date_obj = datetime.strptime(date_key, '%Y-%m-%d')
                                month_key = date_obj.strftime('%Y-%m')
                            elif len(date_key) <= 8 and '-' in date_key:  # Mon-YYYY format
                                month_name, year = date_key.split('-')
                                month_num = list(calendar.month_abbr).index(month_name)
                                month_key = f"{year}-{month_num:02d}"
                            else:
                                continue
                                
                            # Aggregate data by month
                            for metric in selected_metrics:
                                if date_key in date_data and metric in date_data[date_key]:
                                    monthly_data[month_key][metric].extend(date_data[date_key][metric])
                                    
                        except (ValueError, IndexError):
                            continue
                    
                    # Sort months and create labels
                    sorted_months = sorted(monthly_data.keys())[-24:]  # Last 24 months max
                    
                    chart_labels = []
                    for month_key in sorted_months:
                        try:
                            month_obj = datetime.strptime(month_key, '%Y-%m')
                            chart_labels.append(month_obj.strftime('%b %Y'))  # Jan 2024
                        except ValueError:
                            chart_labels.append(month_key)
                    
                    # Use monthly data for metrics calculation
                    date_data = monthly_data
                    final_date_keys = sorted_months
                
                # Create a dataset for each selected metric
                colors = ['#59E6F6', '#F59E0B', '#10B981', '#EF4444', '#8B5CF6', '#F97316']
                
                for i, metric in enumerate(selected_metrics[:6]):  # Limit to 6 metrics for readability
                    metric_label = table_fields.get(metric, metric.replace('_', ' ').title())
                    chart_values = []
                    
                    for date_key in final_date_keys:
                        if date_key in date_data and metric in date_data[date_key]:
                            metric_values = date_data[date_key][metric]
                        else:
                            metric_values = []
                            
                        if metric_values:
                            if aggregation == 'sum':
                                agg_value = sum(metric_values)
                            elif aggregation == 'avg':
                                agg_value = sum(metric_values) / len(metric_values)
                            elif aggregation == 'max':
                                agg_value = max(metric_values)
                            elif aggregation == 'min':
                                agg_value = min(metric_values)
                            elif aggregation == 'count':
                                agg_value = len(metric_values)
                            else:
                                agg_value = sum(metric_values) / len(metric_values)  # Default to average for time series
                            
                            chart_values.append(round(agg_value, 2))
                        else:
                            chart_values.append(0)
                    
                    aggregation_label = aggregation.capitalize()
                    dataset_label = f'{aggregation_label} {metric_label}'
                    
                    color = colors[i % len(colors)]
                    # Create background color with opacity
                    if color == '#59E6F6':
                        bg_color = 'rgba(89, 230, 246, 0.1)'
                    elif color == '#F59E0B':
                        bg_color = 'rgba(245, 158, 11, 0.1)'
                    elif color == '#10B981':
                        bg_color = 'rgba(16, 185, 129, 0.1)'
                    elif color == '#EF4444':
                        bg_color = 'rgba(239, 68, 68, 0.1)'
                    elif color == '#8B5CF6':
                        bg_color = 'rgba(139, 92, 246, 0.1)'
                    elif color == '#F97316':
                        bg_color = 'rgba(249, 115, 22, 0.1)'
                    else:
                        bg_color = 'rgba(89, 230, 246, 0.1)'
                    
                    datasets.append({
                        'label': dataset_label,
                        'data': chart_values,
                        'borderColor': color, 
                        'backgroundColor': bg_color,
                        'tension': 0.4
                    })
                
                response_data['chart_data'] = {
                    'labels': chart_labels,
                    'datasets': datasets
                }
        else:
            # No data found
            response_data['summary'] = {
                'totalProperties': 0,
                'totalUnits': 0,
                'occupiedUnits': 0,
                'avgOccupancy': 0,
                'totalMoveIns': 0,
                'totalMoveOuts': 0,
                'totalApplications': 0,
                'vacantUnits': 0
            }
            response_data['insights'] = ['No data found matching the selected filters.']
            response_data['chart_data'] = {
                'labels': [],
                'datasets': [{
                    'label': 'No Data',
                    'data': [],
                    'borderColor': '#59E6F6',
                    'backgroundColor': 'rgba(89, 230, 246, 0.1)'
                }]
            }
        
        return JsonResponse(response_data)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'error': f'Analysis error: {str(e)}'}, status=500)


def parse_period(period):
	# period: 'Jun-2025', '2025', '2025-Q1', etc.
	if re.match(r'\d{4}-Q[1-4]', period):
		year, q = period.split('-Q')
		q = int(q)
		months = {
			1: ['Jan', 'Feb', 'Mar'],
			2: ['Apr', 'May', 'Jun'],
			3: ['Jul', 'Aug', 'Sep'],
			4: ['Oct', 'Nov', 'Dec']
		}[q]
		return year, months
	elif re.match(r'\d{4}', period):
		return period, None
	elif re.match(r'[A-Za-z]{3}-\d{4}', period):
		m, y = period.split('-')
		return y, [m]
	return None, None

@login_required
def dashboard(request):
	user = request.user
	# Get available periods for dropdown (hierarchical: years > quarters > months)
	all_dates = OlympusLeaseTrendAnalysis.objects.values_list('snapshotdate', flat=True)
	periods = {}
	for d in all_dates:
		m1 = re.match(r'(\d{4})[-/](\d{2})[-/](\d{2})', d)
		if m1:
			year = m1.group(1)
			month = m1.group(2)
			month_name = datetime.strptime(month, "%m").strftime("%b")
			periods.setdefault(year, {"months": set(), "quarters": {}})
			periods[year]["months"].add(month_name)
			# Assign month to quarter
			q = (int(month)-1)//3 + 1
			periods[year]["quarters"].setdefault(f"Q{q}", set()).add(month_name)
		else:
			m2 = re.match(r'([A-Za-z]{3})-(\d{4})', d)
			if m2:
				month_name, year = m2.groups()
				periods.setdefault(year, {"months": set(), "quarters": {}})
				periods[year]["months"].add(month_name)
				q = (datetime.strptime(month_name, "%b").month-1)//3 + 1
				periods[year]["quarters"].setdefault(f"Q{q}", set()).add(month_name)
	# Sort years descending, months/quarters ascending
	sorted_periods = {}
	for year in sorted(periods.keys(), reverse=True):
		sorted_periods[year] = {
			"months": sorted(periods[year]["months"], key=lambda m: datetime.strptime(m, "%b")),
			"quarters": {q: sorted(periods[year]["quarters"][q], key=lambda m: datetime.strptime(m, "%b")) for q in sorted(periods[year]["quarters"].keys())}
		}
	# Read period mode and explicit params (new API)
	period_mode = request.GET.get('period_mode') or ''
	period_year = request.GET.get('period_year') or ''
	period_quarter = request.GET.get('period_quarter') or ''
	period_month = request.GET.get('period_month') or ''
	# Backwards compat: if legacy `period` provided but mode not set, try to infer
	legacy_period = request.GET.get('period')
	if not period_mode and legacy_period:
		# crude inference
		if re.match(r'^\d{4}$', legacy_period):
			period_mode = 'year'; period_year = legacy_period
		elif re.match(r'^[A-Za-z]{3}-\d{4}$', legacy_period) or re.match(r'^[A-Za-z]{3} \d{4}$', legacy_period):
			period_mode = 'month'; period_month = legacy_period
		elif re.match(r'^Q\d[-_]?\d{4}$', legacy_period, re.I):
			period_mode = 'quarter'; period_quarter = legacy_period

	# If still no explicit mode, default to month (existing behavior)
	if not period_mode:
		period_mode = 'month'

	# If no specific period provided, choose latest available month as before
	selected_period = legacy_period
	if not (period_year or period_quarter or period_month or selected_period) and sorted_periods:
		latest_year = next(iter(sorted(sorted_periods.keys(), reverse=True)))
		latest_quarter = next(iter(sorted(sorted_periods[latest_year]['quarters'].keys(), reverse=True)))
		latest_month = sorted(sorted_periods[latest_year]['quarters'][latest_quarter], key=lambda m: datetime.strptime(m, "%b"))[-1]
		selected_period = f"{latest_month}-{latest_year}"
		period_mode = 'month'; period_month = selected_period

	qs = OlympusLeaseTrendAnalysis.objects.all()
	# Apply additional filters (investor, regional_manager, community) early so snapshot selection respects them
	selected_investor = request.GET.get('investor','')
	selected_regional_manager = request.GET.get('regional_manager','')
	selected_community = request.GET.get('community','')
	if selected_investor:
		qs = qs.filter(investor=selected_investor)
	if selected_regional_manager:
		qs = qs.filter(regional_area_manager=selected_regional_manager)
	if selected_community:
		qs = qs.filter(property_name=selected_community)

	last_day_qs = qs.none()

	# Helpers
	month_num_map = {1:'01',2:'02',3:'03',4:'04',5:'05',6:'06',7:'07',8:'08',9:'09',10:'10',11:'11',12:'12'}
	month_name_map = {1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'}

	def pick_last_day_from_qs(filtered_qs):
		if not filtered_qs.exists():
			return qs.none()
		max_date = filtered_qs.aggregate(max_date=Max('snapshotdate'))['max_date']
		return filtered_qs.filter(snapshotdate=max_date) if max_date else qs.none()

	# Build filtered queryset according to mode
	if period_mode == 'year' and period_year:
		# Match both 'YYYY-MM-DD...' and 'Mon-YYYY' patterns
		year = period_year
		year_q = (Q(snapshotdate__startswith=f"{year}-") | Q(snapshotdate__endswith=f"-{year}"))
		year_qs = qs.filter(year_q)
		last_day_qs = pick_last_day_from_qs(year_qs)
		# set selected_period to latest day repr if not provided
		if last_day_qs.exists() and not selected_period:
			selected_period = last_day_qs.first().snapshotdate

	elif period_mode == 'quarter' and period_quarter:
		# period_quarter expected like 'Q1-2024' or 'Q1_2024'
		parts = re.split('[-_]', period_quarter)
		qpart = parts[0].upper()
		year = parts[-1]
		qnum = int(re.sub('[^0-9]','', qpart))
		# months for quarter
		start_month = (qnum-1)*3 + 1
		months_nums = [ month_num_map[m] for m in range(start_month, start_month+3) ]
		months_names = [ month_name_map[m] for m in range(start_month, start_month+3) ]
		q_filter = Q()
		for mnum, mname in zip(months_nums, months_names):
			q_filter |= Q(snapshotdate__startswith=f"{year}-{mnum}") | Q(snapshotdate__icontains=f"{mname}-{year}")
		quarter_qs = qs.filter(q_filter)
		last_day_qs = pick_last_day_from_qs(quarter_qs)
		if last_day_qs.exists() and not selected_period:
			selected_period = last_day_qs.first().snapshotdate

	else:
		# Default: month selection. Consider explicit period_month, or selected_period, or period_year fallback
		month_sel = period_month or selected_period
		m = None
		if month_sel:
			# normalize forms like 'Jan-2024' or 'Jan- 2024'
			mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', month_sel)
			if mm:
				month_name, year = mm.groups()
				month_num = datetime.strptime(month_name, "%b").strftime("%m")
				month_str = f"{year}-{month_num}"
				month_qs = qs.filter(Q(snapshotdate__startswith=month_str) | Q(snapshotdate__icontains=f"{month_name}-{year}"))
				last_day_qs = pick_last_day_from_qs(month_qs)
				selected_period = f"{month_name}-{year}"
		# If still empty and period_year provided, try to pick last day in the year
		if not last_day_qs.exists() and period_year:
			year = period_year
			year_qs = qs.filter(Q(snapshotdate__startswith=f"{year}-") | Q(snapshotdate__endswith=f"-{year}"))
			last_day_qs = pick_last_day_from_qs(year_qs)

	# If still no data, leave last_day_qs empty

	# Compute KPIs and table from last_day_qs
	total_properties = 0
	total_units = 0
	all_properties = []
	occupancy = 0
	if last_day_qs.exists():
		unique_properties = last_day_qs.values('property_number', 'property_name', 'total_units').distinct()
		total_properties = unique_properties.count()
		total_units = sum([row['total_units'] for row in unique_properties])
		all_properties = list(unique_properties)
		occupancy = last_day_qs.aggregate(occ=Avg('percentage_occupacy'))['occ'] or 0

	# Table data (paginated)
	property_list = last_day_qs.values(
		'property_number',
		'property_name',
		'total_units',
		'investor',
		'regional_area_manager',
		'regional_director',
		'asst_manager',
		'occupied_units'
	).distinct()
	paginator = Paginator(list(property_list), 10)
	page_number = request.GET.get('page')
	properties = paginator.get_page(page_number)

	kpi = {
		'total_properties': total_properties,
		'total_units': total_units,
		'occupancy': f"{occupancy:.2f}%",
		'exposure': 0,
		'delinquency': 0,
		'avg_turn_time': 0,
		'service_requests': 0,
		'google_stars': 0,
		'renewal_conversion': 0,
	}

	# Build dependent maps for client-side filtering
	inv_to_regional_map = {}
	inv_reg_to_communities_map = {}
	for row in OlympusLeaseTrendAnalysis.objects.values('investor','regional_area_manager','property_name'):
		inv = row.get('investor') or ''
		rm = row.get('regional_area_manager') or ''
		prop = row.get('property_name') or ''
		inv_to_regional_map.setdefault(inv, set()).add(rm)
		inv_reg_to_communities_map.setdefault((inv,rm), set()).add(prop)
	# Convert sets to sorted lists for JSON-safe context
	inv_to_regional = {k: sorted(list(v)) for k,v in inv_to_regional_map.items()}
	inv_reg_to_communities = {f"{k[0]}|||{k[1]}": sorted(list(v)) for k,v in inv_reg_to_communities_map.items()}

	# Serialize dependent maps as JSON strings for safe insertion into JS
	inv_to_regional_json = json.dumps(inv_to_regional)
	inv_reg_to_communities_json = json.dumps(inv_reg_to_communities)

	# Build context for template rendering
	context = {
		'user': user,
		'properties': properties,
		'kpi': kpi,
		'period_options': sorted_periods,
		'selected_period': selected_period,
		'selected_year': period_year,
		'selected_quarter': period_quarter,
		'selected_month': period_month,
		'selected_period_mode': period_mode,
		'all_properties': all_properties,
		# Provide filter lists
		'investors': OlympusLeaseTrendAnalysis.objects.values_list('investor', flat=True).distinct().order_by('investor'),
		'regional_managers': OlympusLeaseTrendAnalysis.objects.values_list('regional_area_manager', flat=True).distinct().order_by('regional_area_manager'),
		'communities': OlympusLeaseTrendAnalysis.objects.values_list('property_name', flat=True).distinct().order_by('property_name'),
		'inv_to_regional': inv_to_regional,
		'inv_reg_to_communities': inv_reg_to_communities,
		'inv_to_regional_json': inv_to_regional_json,
		'inv_reg_to_communities_json': inv_reg_to_communities_json,
		'selected_investor': request.GET.get('investor',''),
		'selected_regional_manager': request.GET.get('regional_manager',''),
		'selected_community': request.GET.get('community',''),
	}

	# If this is an AJAX fragment request, return rendered fragments as JSON
	is_xhr = request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest' or request.headers.get('x-requested-with') == 'XMLHttpRequest'
	if is_xhr:
		kpi_html = render_to_string('dashboard/partials/_kpi_cards.html', context=context, request=request)
		table_html = render_to_string('dashboard/partials/_property_table.html', context=context, request=request)
		return JsonResponse({'kpi_html': kpi_html, 'table_html': table_html, 'selected_period': selected_period})

	return render(request, 'dashboard/dashboard.html', context)
