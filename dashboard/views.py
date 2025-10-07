from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Sum, Avg, Count, Q, F
from .models import OlympusLeaseTrendAnalysis
from datetime import datetime, date, timedelta
from django.db.models.functions import ExtractYear, ExtractMonth
from .services import DashboardService
from .dashboard_service import DashboardPageService
from django.core.cache import cache
import threading
import calendar
from django.db.models import IntegerField
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
		# Try to reuse cached filter options/maps (single source of truth)
		cache_ttl = 60 * 60  # 1 hour
		cached_filters = cache.get('analytics_filter_options')
		if cached_filters:
			properties = cached_filters.get('properties', [])
			investors = cached_filters.get('investors', [])
			managers = cached_filters.get('managers', [])
			investor_properties = cached_filters.get('investor_properties', {})
			investor_managers = cached_filters.get('investor_managers', {})
			property_managers = cached_filters.get('property_managers', {})
		else:
			# Fetch all distinct (investor, regional_area_manager, property_name) tuples once
			distinct_rows = list(OlympusLeaseTrendAnalysis.objects.values('investor','regional_area_manager','property_name').distinct())
			# Build unique sets
			prop_set = set()
			inv_set = set()
			mgr_set = set()
			investor_properties = {}
			investor_managers = {}
			property_managers = {}
			for row in distinct_rows:
				inv = (row.get('investor') or '').strip()
				rm = (row.get('regional_area_manager') or '').strip()
				prop = (row.get('property_name') or '').strip()
				if prop:
					prop_set.add(prop)
				if inv:
					inv_set.add(inv)
				if rm:
					mgr_set.add(rm)
				# populate mappings
				if inv:
					investor_properties.setdefault(inv, set()).add(prop)
					investor_managers.setdefault(inv, set()).add(rm)
				if prop:
					property_managers.setdefault(prop, set()).add(rm)
			# Convert sets to sorted lists and remove empties
			properties = sorted([p for p in prop_set if p])
			investors = sorted([i for i in inv_set if i])
			managers = sorted([m for m in mgr_set if m])
			investor_properties = {k: sorted([p for p in v if p]) for k, v in investor_properties.items()}
			investor_managers = {k: sorted([m for m in v if m]) for k, v in investor_managers.items()}
			property_managers = {k: sorted([m for m in v if m]) for k, v in property_managers.items()}
			# Cache for faster subsequent page loads
			cache.set('analytics_filter_options', {
				'properties': properties,
				'investors': investors,
				'managers': managers,
				'investor_properties': investor_properties,
				'investor_managers': investor_managers,
				'property_managers': property_managers
			}, timeout=cache_ttl)
		
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

		# Shared date parsing helper used for both cache and DB-derived rows
		def _to_date_generic(dv):
			if dv is None:
				return None
			if isinstance(dv, date):
				return dv
			# string handling: try ISO YYYY-MM-DD first
			try:
				return datetime.strptime(str(dv), '%Y-%m-%d').date()
			except Exception:
				pass
			# try Mon-YYYY like 'Jan-2024' -> return first day of month
			m = str(dv)
			if '-' in m:
				parts = m.split('-')
				if len(parts) == 2 and len(parts[1]) == 4:
					mn = parts[0]
					try:
						month_num = list(calendar.month_abbr).index(mn)
						return date(int(parts[1]), month_num, 1)
					except Exception:
						pass
			# fallback: try parsing generic date
			try:
				return datetime.fromisoformat(str(dv)).date()
			except Exception:
				return None
		
		# Prefer cached full dataset if available to avoid heavy DB queries
		cached_full = cache.get('lease_trend_full_data')
		use_cached = False
		all_data = []
		if cached_full is not None:
			# Filter cached rows in Python according to incoming filters to avoid DB round-trips
			# compute date thresholds
			now = datetime.now().date()
			thirty = now - timedelta(days=30)
			ninety = now - timedelta(days=90)
			six_months = now - timedelta(days=180)
			one_year = now - timedelta(days=365)

			for item in cached_full:
				# Apply filters: properties, investors, managers
				if properties and 'all' not in properties:
					pval = item.get('property_name') or item.get('property_name')
					if pval not in properties:
						continue
				if investors and 'all' not in investors:
					ival = item.get('investor')
					if ival not in investors:
						continue
				if managers and 'all' not in managers:
					mval = item.get('regional_area_manager')
					if mval not in managers:
						continue

				# Date range filtering
				sd = _to_date_generic(item.get('snapshotdate'))
				if sd is None:
					continue
				if date_range == 'last_30_days' and sd < thirty:
					continue
				if date_range == 'last_90_days' and sd < ninety:
					continue
				if date_range == 'last_6_months' and sd < six_months:
					continue
				if date_range == 'last_year' and sd < one_year:
					continue
				if date_range == 'custom' and start_date and end_date:
					# parse provided start/end
					try:
						start_d = datetime.strptime(start_date, '%Y-%m-%d').date()
						ex_d = datetime.strptime(end_date, '%Y-%m-%d').date()
						if sd < start_d or sd > ex_d:
							continue
					except Exception:
						pass

				all_data.append(item)
			use_cached = True

		# If cache was not used, pull data from DB into all_data
		if not use_cached:
			# Get all data and calculate stats manually due to mixed data types
			all_data = list(qs.values())

		# Calculate summary statistics
		# Only proceed if we have any data (either cached or from DB)
		if all_data:
			# Deduplicate by property and use latest snapshot per property to avoid double-counting
			# Build a map of property -> (snapshotdate, row)
			prop_latest = {}
			for item in all_data:
				# key off property_number_id when available, else property_number, else property_name
				pkey = item.get('property_number_id') if item.get('property_number_id') is not None else (item.get('property_number') or item.get('property_name'))
				if pkey is None:
					continue
				# parse snapshot date
				sd = _to_date_generic(item.get('snapshotdate'))
				# Use epoch zero for missing dates to ensure they are treated as older
				if sd is None:
					sd = date(1970,1,1)
				current = prop_latest.get(pkey)
				if current is None or sd > current[0]:
					prop_latest[pkey] = (sd, item)

		total_properties = len(prop_latest)
		# Sum totals from latest-per-property rows only
		total_units = 0
		occupied_units = 0
		occupancy_rates = []
		for sd, row in prop_latest.values():
			total_units += safe_int(row.get('total_units'))
			occupied_units += safe_int(row.get('occupied_units'))
			if row.get('percentage_occupacy') not in (None, '', 'None'):
				occupancy_rates.append(safe_float(row.get('percentage_occupacy')))
		avg_occupancy = sum(occupancy_rates) / len(occupancy_rates) if occupancy_rates else 0

		# Move ins/outs (from latest-per-property rows to avoid double-counting across snapshots)
		move_ins = sum(safe_int(row.get('total_move_ins')) for _, row in prop_latest.values() if row.get('total_move_ins'))
		move_outs = sum(safe_int(row.get('total_move_outs')) for _, row in prop_latest.values() if row.get('total_move_outs'))

		# Applications and other metrics (from latest-per-property rows)
		applications = sum(safe_int(row.get('applications')) for _, row in prop_latest.values() if row.get('applications'))
		vacant_units = sum(safe_int(row.get('vacant_units_without_down_admin')) for _, row in prop_latest.values() if row.get('vacant_units_without_down_admin'))
			
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

		# Include snapshot date when custom date range is used
		if date_range == 'custom' and 'snapshotdate' not in selected_fields:
			selected_fields.insert(0, 'snapshotdate')

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
			
			# Prepare chart labels (groups) - show all properties for zoom/pan functionality
			group_names = list(grouped_data.keys())  # Show all groups, let zoom/pan handle navigation
			chart_labels = group_names
			
			# Create a dataset for each selected metric
			# Palette excludes the warning red by default. Red is applied only when
			# the aggregated values are negative or the metric is inherently negative.
			palette = ['#1E40AF', '#059669', '#5B21B6', '#EA580C', '#7C2D12']  # calm, readable colors
			negative_color = '#DC2626'  # used only for negative/decline metrics
			# Metrics that should generally never be shown as 'red' (positive KPIs)
			always_positive_metrics = set(['percentage_occupacy', 'occupied_units', 'total_units', 'total_move_ins', 'applications', 'effective_rent', 'market_rent', 'avg_amount_per_sqft'])
			# Metrics that are inherently negative indicators
			always_negative_metrics = set(['total_move_outs', 'vacant_units_without_down_admin', 'vacant_units'])

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
				
				# Determine whether this dataset should use the negative color
				negative_flag = any(v < 0 for v in chart_values)
				if metric in always_positive_metrics:
					negative_flag = False
				if metric in always_negative_metrics:
					negative_flag = True

				if negative_flag:
					color = negative_color
				else:
					color = palette[i % len(palette)]

				# Map hex color to rgba background with a higher opacity for visibility
				if color == '#1E40AF':
					bg_color = 'rgba(30, 64, 175, 0.8)'
				elif color == '#059669':
					bg_color = 'rgba(5, 150, 105, 0.8)'
				elif color == '#5B21B6':
					bg_color = 'rgba(91, 33, 182, 0.8)'
				elif color == '#EA580C':
					bg_color = 'rgba(234, 88, 12, 0.8)'
				elif color == '#7C2D12':
					bg_color = 'rgba(124, 45, 18, 0.8)'
				elif color == negative_color:
					bg_color = 'rgba(220, 38, 38, 0.8)'
				else:
					bg_color = 'rgba(30, 64, 175, 0.8)'

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
				# Normalize snapshot date keys to strings so later len() and '-' checks are safe
				raw_date = item.get('snapshotdate')
				if raw_date:
					# Convert date/datetime objects to YYYY-MM-DD strings; leave other strings as-is
					from datetime import date as _date, datetime as _datetime
					if isinstance(raw_date, (_date, _datetime)):
						date_key = raw_date.strftime('%Y-%m-%d')
					else:
						date_key = str(raw_date)
					for metric in selected_metrics:
						# Use .get to avoid KeyError if metric missing
						if metric == 'percentage_occupacy':
							value = safe_float(item.get(metric))
						else:
							value = safe_float(item.get(metric)) if metric in ['avg_amount_per_sqft', 'effective_rent', 'market_rent'] else safe_int(item.get(metric))
						date_data[date_key][metric].append(value)
			
			chart_labels = []
			datasets = []
			
			# Enhanced date handling based on date range
			if date_range == 'last_30_days' or date_range == 'custom':
				# For last 30 days or custom range, show individual dates
				sorted_dates = sorted(date_data.keys())
				
				if date_range == 'custom':
					# For custom range, use the actual date range
					chart_labels = []
					date_range_keys = sorted_dates
					
					for date_key in sorted_dates:
						try:
							if len(date_key) > 8 and '-' in date_key:  # YYYY-MM-DD format
								date_obj = datetime.strptime(date_key, '%Y-%m-%d')
								chart_labels.append(date_obj.strftime('%m/%d/%y'))
							elif len(date_key) <= 8 and '-' in date_key:  # Mon-YYYY format
								month_name, year = date_key.split('-')
								month_num = list(calendar.month_abbr).index(month_name)
								date_obj = datetime(int(year), month_num, 1)
								chart_labels.append(date_obj.strftime('%b %Y'))
							else:
								chart_labels.append(date_key)
						except (ValueError, IndexError):
							chart_labels.append(date_key)
				else:
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
					date_range_keys = date_range_keys
				
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
			palette = ['#1E40AF', '#059669', '#5B21B6', '#EA580C', '#7C2D12']  # calm, readable colors
			negative_color = '#DC2626'
			always_positive_metrics = set(['percentage_occupacy', 'occupied_units', 'total_units', 'total_move_ins', 'applications', 'effective_rent', 'market_rent', 'avg_amount_per_sqft'])
			always_negative_metrics = set(['total_move_outs', 'vacant_units_without_down_admin', 'vacant_units'])

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
				
				negative_flag = any(v < 0 for v in chart_values)
				if metric in always_positive_metrics:
					negative_flag = False
				if metric in always_negative_metrics:
					negative_flag = True

				if negative_flag:
					color = negative_color
				else:
					color = palette[i % len(palette)]

				if color == '#1E40AF':
					bg_color = 'rgba(30, 64, 175, 0.8)'
				elif color == '#059669':
					bg_color = 'rgba(5, 150, 105, 0.8)'
				elif color == '#5B21B6':
					bg_color = 'rgba(91, 33, 182, 0.8)'
				elif color == '#EA580C':
					bg_color = 'rgba(234, 88, 12, 0.8)'
				elif color == '#7C2D12':
					bg_color = 'rgba(124, 45, 18, 0.8)'
				elif color == negative_color:
					bg_color = 'rgba(220, 38, 38, 0.8)'
				else:
					bg_color = 'rgba(30, 64, 175, 0.8)'

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
		
		# Safety: if summary appears empty (all zeros) but we have table rows or raw data,
		# recompute summary from deduplicated latest-per-property rows so UI cards show valid numbers.
		try:
			summary = response_data.get('summary', {})
			if summary and (summary.get('totalUnits') in (0, None)):
				# attempt to rebuild from all_data if available
				if 'all_data' in locals() and all_data:
					prop_latest = {}
					for item in all_data:
						pkey = item.get('property_number_id') if item.get('property_number_id') is not None else (item.get('property_number') or item.get('property_name'))
						if pkey is None:
							continue
						sd = _to_date_generic(item.get('snapshotdate'))
						if sd is None:
							sd = date(1970,1,1)
						cur = prop_latest.get(pkey)
						if cur is None or sd > cur[0]:
							prop_latest[pkey] = (sd, item)
					# compute sums
					total_units = sum(int(r.get('total_units') or 0) for (_, r) in prop_latest.values())
					occupied_units = sum(int(r.get('occupied_units') or 0) for (_, r) in prop_latest.values())
					occupancy_rates = [safe_float(r.get('percentage_occupacy')) for (_, r) in prop_latest.values() if r.get('percentage_occupacy') not in (None, '', 'None')]
					avg_occupancy = sum(occupancy_rates) / len(occupancy_rates) if occupancy_rates else 0
					response_data['summary'].update({
						'totalProperties': len(prop_latest),
						'totalUnits': total_units,
						'occupiedUnits': occupied_units,
						'avgOccupancy': round(avg_occupancy, 1)
					})

					# Activity metrics (move-ins/move-outs/applications) should be
					# aggregated across the selected date window (all_data). Vacant
					# units are a snapshot metric and should come from the latest
					# per-property row.
					move_ins = sum(int((r.get('total_move_ins') or 0)) for r in all_data)
					move_outs = sum(int((r.get('total_move_outs') or 0)) for r in all_data)
					applications = sum(int((r.get('applications') or 0)) for r in all_data)
					vacant_units = sum(int(r.get('vacant_units_without_down_admin') or 0) for (_, r) in prop_latest.values())
					response_data['summary'].update({
						'totalMoveIns': move_ins,
						'totalMoveOuts': move_outs,
						'totalApplications': applications,
						'vacantUnits': vacant_units
					})
		except Exception:
			# if any error here, swallow so we still return the existing response
			import traceback
			traceback.print_exc()

		# Fallback: if chart_data is effectively empty but we have table rows or a populated summary,
		# synthesize a simple chart and friendly insights so the UI isn't left blank.
		try:
			chart = response_data.get('chart_data') or {}
			needs_fallback = False
			if not chart.get('labels') or not chart.get('datasets'):
				needs_fallback = True
			elif isinstance(chart.get('datasets'), list) and len(chart.get('datasets')) > 0 and chart['datasets'][0].get('label') == 'No Data':
				needs_fallback = True
			# Only build fallback when we have table data or a non-empty summary
			if needs_fallback and response_data.get('table_data'):
				# If the request is for last_30_days, prefer to synthesize a daily occupancy
				# time-series from `all_data` so the chart x-axis is dates. Only fall back to
				# a property-based chart if synthesis fails.
				table = response_data['table_data']
				if date_range == 'last_30_days' and all_data:
					try:
						# Build last-30-days keys
						from datetime import datetime as _dt, timedelta as _td
						today = _dt.now().date()
						final_date_keys = []
						chart_labels = []
						for i in range(29, -1, -1):
							d = today - _td(days=i)
							final_date_keys.append(d.strftime('%Y-%m-%d'))
							chart_labels.append(d.strftime('%m/%d'))
						# Aggregate occupancy per day by matching snapshot date or month-year
						metric = 'percentage_occupacy'
						values = []
						last_val = None
						for dk in final_date_keys:
							key_date = None
							try:
								key_date = _dt.strptime(dk, '%Y-%m-%d').date()
							except Exception:
								pass
							# collect matching values from all_data
							matches = []
							for item in all_data:
								raw = item.get('snapshotdate')
								dt_parsed = _to_date_generic(raw)
								if dt_parsed is None:
									continue
								if key_date and (dt_parsed == key_date or (dt_parsed.year == key_date.year and dt_parsed.month == key_date.month)):
									val = safe_float(item.get(metric))
									matches.append(val)
							if matches:
								last_val = sum(matches) / len(matches)
								values.append(round(last_val, 2))
							else:
								values.append(round(last_val or 0, 2))
						# If we got non-zero points, use this time-series
						if any(values):
							response_data['chart_data'] = {
								'labels': chart_labels,
								'datasets': [{
									'label': 'Occupancy %',
									'data': values,
									'borderColor': '#1E40AF',
									'backgroundColor': 'rgba(30, 64, 175, 0.1)',
									'tension': 0.4
								}]
							}
							# Build simple insights from summary
							s = response_data.get('summary', {})
							ins = []
							if s.get('totalProperties'):
								ins.append(f"{s.get('totalProperties'):,} properties included in this analysis.")
							if s.get('totalUnits'):
								ins.append(f"{s.get('totalUnits'):,} total units across the selected set.")
							if s.get('totalMoveIns'):
								ins.append(f"{s.get('totalMoveIns'):,} move-ins in the selected period.")
							if s.get('totalApplications'):
								ins.append(f"{s.get('totalApplications'):,} applications received.")
							if not ins:
								ins.append('Data is available but not enough time-series points to build a trend. Try a wider date range or different grouping.')
							response_data['insights'] = ins
					except Exception:
						# if synthesis fails, fall back to property-based chart below
						import traceback; traceback.print_exc()
				# if synthesis did not set chart_data, proceed to property-based chart
				if not response_data.get('chart_data'):
					# Prefer occupancy percentage if present, otherwise total units per property
					labels = [row.get('Property Name') or row.get('property_name') or f"Property {i+1}" for i, row in enumerate(table[:10])]
					if table and 'Occupancy %' in table[0]:
						values = []
						for row in table[:10]:
							v = row.get('Occupancy %')
							try:
								values.append(float(str(v).replace('%','')))
							except Exception:
								values.append(0)
						dataset_label = 'Occupancy %'
					else:
						values = [int(row.get('Total Units') or 0) for row in table[:10]]
						dataset_label = 'Total Units'
					response_data['chart_data'] = {
						'labels': labels,
						'datasets': [{
							'label': dataset_label,
							'data': values,
							'borderColor': '#1E40AF',
							'backgroundColor': 'rgba(30, 64, 175, 0.1)',
							'tension': 0.4
						}]
					}
				# Build simple insights from summary so the key insights card is useful
				s = response_data.get('summary', {})
				ins = []
				if s.get('totalProperties'):
					ins.append(f"{s.get('totalProperties'):,} properties included in this analysis.")
				if s.get('totalUnits'):
					ins.append(f"{s.get('totalUnits'):,} total units across the selected set.")
				if s.get('totalMoveIns'):
					ins.append(f"{s.get('totalMoveIns'):,} move-ins in the selected period.")
				if s.get('totalApplications'):
					ins.append(f"{s.get('totalApplications'):,} applications received.")
				if not ins:
					ins.append('Data is available but not enough time-series points to build a trend. Try a wider date range or different grouping.')
				response_data['insights'] = ins
		except Exception:
			# Never fail the API for UI rendering issues; swallow and return whatever we have.
			import traceback; traceback.print_exc()
		
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
	# If the user didn't supply any period-related parameters, default to ALL years
	period_params = ['period_mode', 'period_year', 'period_quarter', 'period_month', 'period']
	has_period_param = any([p in request.GET and request.GET.get(p) for p in period_params])
	if not has_period_param:
		# prefer the 'all years' dataset on first landing
		params = {'period_mode': 'year', 'period_year': 'all'}
	else:
		# pass through user-supplied GET parameters
		params = request.GET

	svc = DashboardPageService(params)
	svc_ctx = svc.get_context()

	# Option B: keep KPIs/charts filtered by user selection but present an unfiltered
	# properties list for the table/modals so the client-managed table doesn't get
	# replaced when filters change KPI cards. Build an unfiltered param set by
	# removing filter keys (investor, regional_manager, community) and request an
	# unfiltered properties_list from the same service.
	unfiltered_params = dict(params) if isinstance(params, dict) else dict(params.items())
	for fk in ('investor', 'regional_manager', 'community'):
		unfiltered_params.pop(fk, None)
	svc_unfiltered = DashboardPageService(unfiltered_params)
	svc_unfiltered_ctx = svc_unfiltered.get_context()

	# build inv_reg_to_communities mapping from service-provided maps (use unfiltered maps)
	investor_properties = svc_unfiltered_ctx.get('investor_properties', {})
	property_managers = svc_unfiltered_ctx.get('property_managers', {})
	inv_reg_to_communities = {}
	for inv, props in investor_properties.items():
		for prop in props:
			mgrs = property_managers.get(prop, [])
			for mgr in mgrs:
				key = f"{inv}|||{mgr}"
				inv_reg_to_communities.setdefault(key, []).append(prop)
	# sort lists
	inv_reg_to_communities = {k: sorted(v) for k, v in inv_reg_to_communities.items()}

	context = {
		'user': user,
		'properties': svc_ctx.get('properties_page'),
		'kpi': svc_ctx.get('kpi'),
		'period_options': svc_ctx.get('periods'),
		'selected_period': svc_ctx.get('selected_period'),
		'selected_year': svc_ctx.get('period_year'),
		'selected_quarter': svc_ctx.get('period_quarter'),
		'selected_month': svc_ctx.get('period_month'),
		'selected_period_mode': svc_ctx.get('period_mode'),
		# Use unfiltered properties/dropdown maps so table/modal stay stable across filter changes
		'all_properties': svc_unfiltered_ctx.get('properties_list'),
		'investors': svc_unfiltered_ctx.get('investors'),
		'regional_managers': svc_unfiltered_ctx.get('managers'),
		'communities': svc_unfiltered_ctx.get('properties_list'),
		'inv_to_regional': svc_unfiltered_ctx.get('investor_managers'),
		'inv_reg_to_communities': inv_reg_to_communities,
		'inv_to_regional_json': json.dumps(svc_unfiltered_ctx.get('investor_managers', {})),
		'inv_reg_to_communities_json': json.dumps(inv_reg_to_communities),
		'chart_rent_json': json.dumps(svc_ctx.get('chart_rent', {})),
		'chart_occrev_json': json.dumps(svc_ctx.get('chart_occrev', {})),
	}

	chart_props = dict(svc_ctx.get('chart_properties', {}) or {})

	context['chart_properties_json'] = json.dumps(chart_props)
	context['selected_investor'] = request.GET.get('investor','')
	context['selected_regional_manager'] = request.GET.get('regional_manager','')
	context['selected_community'] = request.GET.get('community','')

	is_xhr = request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest' or request.headers.get('x-requested-with') == 'XMLHttpRequest'
	if is_xhr:
		kpi_html = render_to_string('dashboard/partials/_kpi_cards.html', context=context, request=request)
		table_html = render_to_string('dashboard/partials/_property_table.html', context=context, request=request)
		return JsonResponse({'kpi_html': kpi_html, 'table_html': table_html, 'selected_period': svc_ctx.get('selected_period')})

	return render(request, 'dashboard/dashboard.html', context)
