from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Sum, Avg, Count, Q, F
from .models import OlympusLeaseTrendAnalysis, OlympusLeaseKpisTrendMonthly
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
			cutoff_month = ninety_days_ago.strftime('%b-%Y')
			# Include exact-date rows and month-year formatted rows
			qs = qs.filter(
				Q(snapshotdate__gte=cutoff_date) |
				Q(snapshotdate__contains=cutoff_month)
			)
		elif date_range == 'last_6_months':
			six_months_ago = datetime.now() - timedelta(days=180)
			cutoff_date = six_months_ago.strftime('%Y-%m-%d')
			cutoff_month = six_months_ago.strftime('%b-%Y')
			qs = qs.filter(
				Q(snapshotdate__gte=cutoff_date) |
				Q(snapshotdate__contains=cutoff_month)
			)
		elif date_range == 'current_year':
			# Return rows for the current calendar year up to today. Some rows
			# may be stored as 'Mon-YYYY' strings so include both year-lookup
			# (works for DateField values) and a simple contains fallback.
			this_year = datetime.now().year
			today = datetime.now().date()
			qs = qs.filter(
				Q(snapshotdate__year=this_year, snapshotdate__lte=today) |
				Q(snapshotdate__contains=str(this_year))
			)
		elif date_range == 'last_year':
			# Interpret 'last_year' as the previous calendar year (Jan 1 - Dec 31 of prior year)
			prev_year = datetime.now().year - 1
			# Use __year lookup for DateFields and a contains fallback for string-stored month-year values
			qs = qs.filter(
				Q(snapshotdate__year=prev_year) |
				Q(snapshotdate__contains=str(prev_year))
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
			# Get all data and calculate stats manually due to mixed data types.
			# IMPORTANT: the underlying DB table/view does not have an implicit
			# `id` primary key column. Calling qs.values() without field names
			# causes Django to include the model pk ("id") in the SELECT which
			# raises a ProgrammingError. Explicitly request only fields that
			# exist on the model / DB to avoid that error.
			db_fields = [
				'snapshotdate', 'property_number', 'property_name', 'investor',
				'regional_area_manager', 'regional_director', 'asst_manager',
				'total_units', 'occupied_units', 'percentage_occupacy',
				'total_move_ins', 'total_move_outs', 'applications',
				'vacant_units_without_down_admin', 'effective_rent', 'market_rent',
				'avg_amount_per_sqft'
			]
			all_data = list(qs.values(*db_fields))

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
		# Classification of metrics: snapshot/state metrics need latest-per-property treatment,
		# event/flow metrics should be summed across the selected window.
		SNAPSHOT_METRICS = set(['total_units','occupied_units','vacant_units_without_down_admin','percentage_occupacy','effective_rent','market_rent','avg_amount_per_sqft'])
		EVENT_METRICS = set(['total_move_ins','total_move_outs','applications','visit','cancelled','denied'])

		from collections import defaultdict

		def latest_per_property_map(rows):
			"""Return map property_key -> latest_row (by parsed snapshotdate) from rows."""
			m = {}
			for r in rows:
				pkey = r.get('property_number') or r.get('property_name')
				if pkey is None:
					continue
				sd = _to_date_generic(r.get('snapshotdate'))
				if sd is None:
					sd = date(1970,1,1)
				cur = m.get(pkey)
				if cur is None or sd > cur[0]:
					m[pkey] = (sd, r)
			# return only rows (not the date)
			return {k: v[1] for k, v in m.items()}

		def latest_per_property_as_of(all_rows, as_of_date):
			"""Return list of latest rows per property with snapshotdate <= as_of_date."""
			m = {}
			for r in all_rows:
				pkey = r.get('property_number') or r.get('property_name')
				if pkey is None:
					continue
				sd = _to_date_generic(r.get('snapshotdate'))
				if sd is None:
					continue
				if sd <= as_of_date:
					cur = m.get(pkey)
					if cur is None or sd > cur[0]:
						m[pkey] = (sd, r)
			return [v[1] for v in m.values()]

		# Helper to aggregate a metric for a group of rows
		def aggregate_metric_for_group(rows, metric, aggregation_method):
			# rows: raw rows for group (all snapshots within window)
			# For snapshot metrics, operate on latest-per-property rows
			if metric in SNAPSHOT_METRICS:
				latest_rows = list(latest_per_property_map(rows).values())
				# Special handling for occupancy %: compute weighted portfolio occupancy
				if metric == 'percentage_occupacy':
					total_units = sum(safe_int(r.get('total_units')) for r in latest_rows)
					occupied = sum(safe_int(r.get('occupied_units')) for r in latest_rows)
					if aggregation_method == 'count':
						return len(latest_rows)
					# For sum/avg: return weighted occupancy percentage
					return (occupied / total_units * 100) if total_units else 0
				# For other snapshot numeric metrics
				values = []
				for r in latest_rows:
					val = r.get(metric)
					if val is None or val == '':
						continue
					if metric in ['avg_amount_per_sqft','effective_rent','market_rent']:
						values.append(safe_float(val))
					else:
						values.append(safe_int(val))
			else:
				# Event metrics operate over all rows in the window for the group
				values = []
				for r in rows:
					val = r.get(metric)
					if val is None or val == '':
						continue
					if metric in ['avg_amount_per_sqft','effective_rent','market_rent']:
						values.append(safe_float(val))
					else:
						values.append(safe_int(val))

			if not values:
				return 0
			if aggregation_method == 'sum':
				return sum(values)
			if aggregation_method == 'avg':
				return sum(values) / len(values)
			if aggregation_method == 'max':
				return max(values)
			if aggregation_method == 'min':
				return min(values)
			if aggregation_method == 'count':
				# For snapshot metrics, count distinct properties (values built from latest_rows)
				if metric in SNAPSHOT_METRICS:
					return len(latest_per_property_map(rows))
				# For event metrics, count rows
				return len(values)
			# default
			return sum(values)

		if group_by and group_by in table_fields:
			# Group data for chart
			grouped_data = defaultdict(list)

			for item in all_data:
				group_value = str(item.get(group_by) or '')
				if group_value:
					grouped_data[group_value].append(item)

			chart_labels = list(grouped_data.keys())
			datasets = []
			selected_metrics = metrics if metrics else ['percentage_occupacy']
			palette = ['#1E40AF', '#059669', '#5B21B6', '#EA580C', '#7C2D12']
			negative_color = '#DC2626'
			always_positive_metrics = set(['percentage_occupacy', 'occupied_units', 'total_units', 'total_move_ins', 'applications', 'effective_rent', 'market_rent', 'avg_amount_per_sqft'])
			always_negative_metrics = set(['total_move_outs', 'vacant_units_without_down_admin', 'vacant_units'])

			for i, metric in enumerate(selected_metrics[:6]):
				metric_label = table_fields.get(metric, metric.replace('_', ' ').title())
				chart_values = []
				for group_name in chart_labels:
					rows = grouped_data.get(group_name, [])
					val = aggregate_metric_for_group(rows, metric, aggregation)
					chart_values.append(round(val, 2))

				aggregation_label = aggregation.capitalize()
				dataset_label = f'{aggregation_label} {metric_label}'
				negative_flag = any(v < 0 for v in chart_values)
				if metric in always_positive_metrics:
					negative_flag = False
				if metric in always_negative_metrics:
					negative_flag = True
				color = negative_color if negative_flag else palette[i % len(palette)]
				bg_color = 'rgba(30, 64, 175, 0.8)'
				if color == '#059669': bg_color = 'rgba(5, 150, 105, 0.8)'
				elif color == '#5B21B6': bg_color = 'rgba(91, 33, 182, 0.8)'
				elif color == '#EA580C': bg_color = 'rgba(234, 88, 12, 0.8)'
				elif color == '#7C2D12': bg_color = 'rgba(124, 45, 18, 0.8)'

				datasets.append({
					'label': dataset_label,
					'data': chart_values,
					'borderColor': color,
					'backgroundColor': bg_color,
				})
		
			# Create a dataset for each selected metric
			palette = ['#1E40AF', '#059669', '#5B21B6', '#EA580C', '#7C2D12']  # calm, readable colors
			negative_color = '#DC2626'
			always_positive_metrics = set(['percentage_occupacy', 'occupied_units', 'total_units', 'total_move_ins', 'applications', 'effective_rent', 'market_rent', 'avg_amount_per_sqft'])
			always_negative_metrics = set(['total_move_outs', 'vacant_units_without_down_admin', 'vacant_units'])

			for i, metric in enumerate(selected_metrics[:6]):  # Limit to 6 metrics for readability
				metric_label = table_fields.get(metric, metric.replace('_', ' ').title())
				chart_values = []
				# ensure date_rows and final_date_keys exist (some fallback branches build them earlier)
				if 'final_date_keys' not in locals():
					final_date_keys = []
				if 'date_rows' not in locals():
					date_rows = {}
				for date_key in final_date_keys:
					# For each date bucket, collect rows and compute aggregate using helpers
					rows_for_bucket = date_rows.get(date_key, []) if date_rows else []
					if not rows_for_bucket:
						chart_values.append(0)
						continue
					# If metric is a snapshot metric, compute latest-as-of for that date and aggregate
					try:
						# convert date_key to date for comparisons when needed
						as_of = None
						if isinstance(date_key, str) and len(date_key) >= 8 and '-' in date_key:
							try:
								as_of = datetime.strptime(date_key, '%Y-%m-%d').date()
							except Exception:
								as_of = None
						# For monthly buckets, date_key may be 'YYYY-MM' — treat as month-end
						if as_of is None and isinstance(date_key, str) and re.match(r'^\d{4}-\d{2}$', str(date_key)):
							try:
								month_end = datetime.strptime(date_key + '-01', '%Y-%m-%d')
								# move to last day of month
								last_day = calendar.monthrange(month_end.year, month_end.month)[1]
								as_of = date(month_end.year, month_end.month, last_day)
							except Exception:
								as_of = None
						# If we have an as_of date, get latest rows as-of that date
						if as_of:
							bucket_rows = latest_per_property_as_of(all_data, as_of)
						else:
							# fallback: use the rows that are in the bucket
							bucket_rows = rows_for_bucket
						val = aggregate_metric_for_group(bucket_rows, metric, aggregation)
					except Exception:
						# on any error, fallback to previous numeric-list approach (safe)
						try:
							metric_values = []
							for r in rows_for_bucket:
								if metric == 'percentage_occupacy':
									metric_values.append(safe_float(r.get(metric)))
								else:
									metric_values.append(safe_int(r.get(metric)))
							if metric_values:
								val = sum(metric_values) if aggregation == 'sum' else (sum(metric_values) / len(metric_values) if aggregation == 'avg' else len(metric_values) if aggregation == 'count' else sum(metric_values))
							else:
								val = 0
						except Exception:
							val = 0
					chart_values.append(round(val, 2))
				
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
	# If the user didn't supply any period-related parameters, default to the
	# latest available month (server-side) rather than the full 'all years' set.
	period_params = ['period_mode', 'period_year', 'period_quarter', 'period_month', 'period']
	has_period_param = any([p in request.GET and request.GET.get(p) for p in period_params])
	if not has_period_param:
		# Inspect available periods via the DashboardService so the server picks
		# the latest month present in the DB. Fall back to 'all years' when no
		# month data is available (e.g., empty DB during local dev).
		svc_probe = DashboardPageService({})
		svc_periods = svc_probe.get_context().get('periods') or {}
		# periods structure: { '2025': { 'months': [...], 'quarters': {...} }, ... }
		if svc_periods:
			# choose newest year then newest month within that year's quarters
			try:
				latest_year = next(iter(sorted(svc_periods.keys(), reverse=True)))
				# Find latest month by traversing quarters (quarters contain month names)
				quarters = svc_periods.get(latest_year, {}).get('quarters', {})
				if quarters:
					latest_quarter = next(iter(sorted(quarters.keys(), reverse=True)))
					months = quarters.get(latest_quarter, [])
					if months:
						latest_month = sorted(months, key=lambda m: datetime.strptime(m, '%b'))[-1]
						params = {'period_mode': 'month', 'period_month': f"{latest_month}-{latest_year}"}
					else:
						# no months found -> fall back to all years
						params = {'period_mode': 'year', 'period_year': 'all'}
				else:
					# no quarters -> try months top-level (compat)
					months_top = svc_periods.get(latest_year, {}).get('months') or []
					if months_top:
						latest_month = sorted(months_top, key=lambda m: datetime.strptime(m, '%b'))[-1]
						params = {'period_mode': 'month', 'period_month': f"{latest_month}-{latest_year}"}
					else:
						params = {'period_mode': 'year', 'period_year': 'all'}
			except Exception:
				params = {'period_mode': 'year', 'period_year': 'all'}
		else:
			# no period data available at all; preserve previous default
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

	# Build renewals chart from monthly KPIs table (if available). This yields
	# labels + two datasets: Expirations and Renewals. We keep a safe fallback
	# in case the monthly table is missing or the query fails (so local dev
	# without the production table won't crash the dashboard).
	# Consolidated monthly KPI aggregation: build one grouped query (year or year+month)
	try:
		from django.db.models import Sum, Avg, Count
		monthly_qs = OlympusLeaseKpisTrendMonthly.objects.all()
		# Apply basic filters matching the UI (now supporting multi-select arrays)
		inv_list = request.GET.getlist('investor') if request.GET.getlist('investor') else ([params.get('investor')] if params.get('investor') else [])
		regional_list = request.GET.getlist('regional_manager') if request.GET.getlist('regional_manager') else ([params.get('regional_manager')] if params.get('regional_manager') else [])
		community_list = request.GET.getlist('community') if request.GET.getlist('community') else ([params.get('community')] if params.get('community') else [])
		
		# Filter with arrays (__in lookup)
		if inv_list and inv_list[0]:  # ensure not empty string
			monthly_qs = monthly_qs.filter(investor__in=inv_list)
		if regional_list and regional_list[0]:
			monthly_qs = monthly_qs.filter(regional_area_manager__in=regional_list)
		if community_list and community_list[0]:
			monthly_qs = monthly_qs.filter(property_name__in=community_list)

		# Business rule: exclude BLACKSTONE/LIVCOR for periods after June 2025
		# unless the user explicitly filtered by investor.
		try:
			def _period_after_jun_2025_local(mode, year, months_list, quarter):
				import re, calendar
				try:
					if mode == 'month' and months_list:
						# months_list may be a list like ['Oct'] or a single selected period string
						mval = months_list if isinstance(months_list, str) else (months_list[0] if months_list else '')
						mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', mval)
						if mm:
							mon_abbr, yr = mm.groups()
							mon_num = list(calendar.month_abbr).index(mon_abbr)
							yr = int(yr)
							return (yr > 2025) or (yr == 2025 and mon_num > 6)
					if mode == 'quarter' and quarter:
						mm = re.search(r'Q(\d)', quarter, re.I)
						yy = re.search(r'(\d{4})', quarter)
						if mm and yy:
							qnum = int(mm.group(1))
							yr = int(yy.group(1))
							return (yr > 2025) or (yr == 2025 and qnum >= 3)
					if mode == 'year' and year and str(year).isdigit():
						return int(year) > 2025
				except Exception:
					return False
				return False

			sel_mode = svc_ctx.get('period_mode') or (params.get('period_mode') if hasattr(params, 'get') else None)
			sel_period = svc_ctx.get('selected_period') or svc_ctx.get('period_month') or svc_ctx.get('period_quarter') or svc_ctx.get('period_year')
			year, months = parse_period(sel_period) if sel_period else (None, None)
			if _period_after_jun_2025_local(sel_mode, year, sel_period, svc_ctx.get('period_quarter')) and not inv:
				monthly_qs = monthly_qs.exclude(investor__iexact='BLACKSTONE/LIVCOR')
		except Exception:
			pass

		# Period selection and bucket decision
		# Prefer the service-provided `selected_period`. If that is missing (the
		# PageService may set `period_month`/`period_quarter` instead), fall back
		# to those fields so the monthly aggregation is narrowed correctly.
		sel_period = svc_ctx.get('selected_period')
		if not sel_period:
			# try explicit month/quarter/year values from the service context
			sel_period = svc_ctx.get('period_month') or svc_ctx.get('period_quarter') or svc_ctx.get('period_year')
		sel_mode = svc_ctx.get('period_mode') or (params.get('period_mode') if hasattr(params, 'get') else None)
		
		# Support multi-month: get array of selected months when in month mode
		selected_months = []
		if sel_mode == 'month':
			if hasattr(params, 'getlist'):
				selected_months = params.getlist('period_month')
			if not selected_months and sel_period:
				selected_months = [sel_period]
		
		# Support multi-quarter: get array of selected quarters when in quarter mode
		selected_quarters = []
		if sel_mode == 'quarter':
			# Use request.GET directly to ensure we get all query parameters
			if hasattr(request.GET, 'getlist'):
				selected_quarters = request.GET.getlist('period_quarter')
			elif hasattr(params, 'getlist'):
				selected_quarters = params.getlist('period_quarter')
			if not selected_quarters and sel_period:
				selected_quarters = [sel_period]
		
		# Support multi-year: get array of selected years when in year mode
		selected_years = []
		if sel_mode == 'year':
			# Use request.GET directly to ensure we get all query parameters
			if hasattr(request.GET, 'getlist'):
				selected_years = request.GET.getlist('period_year')
			elif hasattr(params, 'getlist'):
				selected_years = params.getlist('period_year')
			if not selected_years and sel_period:
				selected_years = [sel_period]
		
		year, months = parse_period(sel_period) if sel_period else (None, None)
		import calendar as _calendar
		# Show yearly grouping only when period_year is 'all' or multiple years are selected
		# For a single specific year, show monthly breakdown
		show_by_year = (sel_mode == 'year' and (not selected_years or len(selected_years) > 1 or (selected_years and selected_years[0] == 'all')))

		# Narrow the queryset when a specific year/months are requested
		if not show_by_year:
			if sel_mode == 'month' and selected_months:
				# Multi-month support: filter by all selected months using OR
				from django.db.models import Q
				q_filter = Q()
				for pm in selected_months:
					mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', pm)
					if mm:
						month_name, yr = mm.groups()
						try:
							month_num = list(_calendar.month_abbr).index(month_name)
							q_filter |= Q(enddateofmonth__month=month_num, enddateofmonth__year=int(yr))
						except Exception:
							pass
				if q_filter:
					monthly_qs = monthly_qs.filter(q_filter)
			elif sel_mode == 'quarter' and selected_quarters:
				# Multi-quarter support: filter by all selected quarters using OR
				from django.db.models import Q
				q_filter = Q()
				for pq in selected_quarters:
					parts = re.split('[-_]', pq)
					qpart = parts[0].upper()
					yr = parts[-1]
					qnum = int(re.sub('[^0-9]', '', qpart))
					start_month = (qnum - 1) * 3 + 1
					months_in_quarter = [start_month, start_month + 1, start_month + 2]
					for month_num in months_in_quarter:
						q_filter |= Q(enddateofmonth__month=month_num, enddateofmonth__year=int(yr))
				if q_filter:
					monthly_qs = monthly_qs.filter(q_filter)
			elif sel_mode == 'year' and selected_years and len(selected_years) == 1:
				# Single year selected: filter to that year and show monthly breakdown
				yr = selected_years[0]
				if yr and yr != 'all':
					try:
						monthly_qs = monthly_qs.filter(enddateofmonth__year=int(yr))
					except Exception:
						pass
			elif year and months:
				month_nums = []
				for m in months:
					try:
						month_nums.append(list(_calendar.month_abbr).index(m))
					except Exception:
						pass
				if month_nums:
					monthly_qs = monthly_qs.filter(enddateofmonth__month__in=month_nums, enddateofmonth__year=int(year))
				else:
					monthly_qs = monthly_qs.filter(enddateofmonth__year=int(year))
			elif year and year != 'all':
				try:
					monthly_qs = monthly_qs.filter(enddateofmonth__year=int(year))
				except Exception:
					pass
		else:
			# show_by_year is True: filter by multiple years or 'all'
			if sel_mode == 'year' and selected_years:
				# Multi-year support: filter by all selected years using OR
				from django.db.models import Q
				q_filter = Q()
				for yr in selected_years:
					if yr and yr != 'all':
						try:
							q_filter |= Q(enddateofmonth__year=int(yr))
						except Exception:
							pass
				if q_filter:
					monthly_qs = monthly_qs.filter(q_filter)

		# Build grouped aggregation that returns all needed fields per bucket
		if show_by_year:
			grouped = (monthly_qs
					 .annotate(year=ExtractYear('enddateofmonth'))
					 .values('year')
					 .annotate(cnt=Count('property_number'),
					           expirations_sum=Sum('expirations'),
					           renewed_sum=Sum('renewed'),
					           controllable_sum=Sum('controllable_expense'),
					           non_controllable_sum=Sum('non_controllable_expense'),
					           service_requests_sum=Sum('service_request'),
					           exposure_sum=Sum('exposure'),
					           delinquency_sum=Sum('delinquency'),
					           renewal_conv_avg=Avg('renewel_conversion'),
					           avg_turn=Avg('average_turn_time'))
					 .order_by('year'))
			# When multiple years selected, show all; otherwise show last 6
			if selected_years and len(selected_years) > 1:
				rows = list(grouped)
			else:
				rows = list(grouped)[-6:]
		else:
			grouped = (monthly_qs
				 .annotate(year=ExtractYear('enddateofmonth'), month=ExtractMonth('enddateofmonth'))
				 .values('year', 'month')
				 .annotate(cnt=Count('property_number'),
				           expirations_sum=Sum('expirations'),
				           renewed_sum=Sum('renewed'),
				           controllable_sum=Sum('controllable_expense'),
				           non_controllable_sum=Sum('non_controllable_expense'),
				           service_requests_sum=Sum('service_request'),
				           exposure_sum=Sum('exposure'),
				           delinquency_sum=Sum('delinquency'),
				           renewal_conv_avg=Avg('renewel_conversion'),
				           avg_turn=Avg('average_turn_time'))
				 .order_by('year', 'month'))
			# When multiple quarters/months selected, show all; otherwise show last 6
			if (selected_quarters and len(selected_quarters) > 1) or (selected_months and len(selected_months) > 1):
				rows = list(grouped)
			else:
				rows = list(grouped)[-6:]

		# Build charts and KPI aggregates from rows
		labels = []
		expirations = []
		renewals = []
		elabels = []
		controllable = []
		non_controllable = []
		# KPI accumulator
		total_service_requests = 0
		total_exposure = 0
		total_delinquency = 0
		# For pooled averages (avg_turn_time, renewal_conv) compute numerator/denominator
		turn_num = 0.0
		turn_den = 0
		rc_num = 0.0
		rc_den = 0
		from datetime import datetime as _dt
		for r in rows:
			if show_by_year:
				y = int(r.get('year') or 0)
				labels.append(str(y) if y else '')
			else:
				y = int(r.get('year') or 0)
				m = int(r.get('month') or 0)
				if y and m:
					try:
						labels.append(_dt(y, m, 1).strftime('%b-%y'))
					except Exception:
						labels.append(f"{m}-{y}")
				else:
					labels.append('')

			expirations.append(int(r.get('expirations_sum') or 0))
			renewals.append(int(r.get('renewed_sum') or 0))

			# expense labels mirror renewals labels
			elabels.append(labels[-1])
			controllable.append(round((r.get('controllable_sum') or 0) / 1000.0, 2))
			non_controllable.append(round((r.get('non_controllable_sum') or 0) / 1000.0, 2))

			# KPIs accumulation
			total_service_requests += int(r.get('service_requests_sum') or 0)
			total_exposure += int(r.get('exposure_sum') or 0)
			total_delinquency += int(r.get('delinquency_sum') or 0)

			cnt = int(r.get('cnt') or 0)
			avg_turn = r.get('avg_turn')
			if avg_turn is not None and cnt:
				turn_num += float(avg_turn) * cnt
				turn_den += cnt
			rc = r.get('renewal_conv_avg')
			if rc is not None and cnt:
				rc_num += float(rc) * cnt
				rc_den += cnt

		# Build chart payloads
		chart_renewals = {
			'labels': labels,
			'datasets': [
				{'label': 'Expirations', 'data': expirations, 'backgroundColor': '#59E6F6', 'borderColor': '#59E6F6'},
				{'label': 'Renewals', 'data': renewals, 'backgroundColor': '#0E555A', 'borderColor': '#0E555A'}
			]
		}

		chart_expense = {
			'labels': elabels,
			'datasets': [
				{'label': 'Controllable', 'data': controllable, 'backgroundColor': '#0E555A'},
				{'label': 'Non-Controllable', 'data': non_controllable, 'backgroundColor': '#C69A58'}
			]
		}

		# Compute KPI overrides
		kpi_overrides = {}
		kpi_overrides['service_requests'] = int(total_service_requests)
		kpi_overrides['exposure'] = int(total_exposure)
		kpi_overrides['delinquency'] = int(total_delinquency)
		if turn_den:
			kpi_overrides['avg_turn_time'] = round(turn_num / turn_den, 1)
		if rc_den:
			kpi_overrides['renewal_conversion'] = round(rc_num / rc_den, 1)

		# NOTE: KPI overrides and chart payloads will be applied to the template context
		# after the primary context dict is built further below. We store them in
		# local variables here (chart_renewals, chart_expense, kpi_overrides).

	except Exception:
		# On any failure, provide empty chart payloads and no KPI overrides
		chart_renewals = {'labels': [], 'datasets': []}
		chart_expense = {'labels': [], 'datasets': []}
		kpi_overrides = {}

	# Build Move-Out Reasons chart from monthly moveout reasons table.
	# initialize with default to ensure context serialization can't fail
	chart_moveout = {'labels': [], 'datasets': []}
	try:
		from django.db.models import Sum
		from dashboard.models import OlympusLeaseMoveoutReasonsTrendMonthly
		mo_qs = OlympusLeaseMoveoutReasonsTrendMonthly.objects.all()
		# apply same basic filters
		inv = params.get('investor') if hasattr(params, 'get') else params.get('investor', '')
		regional = params.get('regional_manager') if hasattr(params, 'get') else params.get('regional_manager', '')
		community = params.get('community') if hasattr(params, 'get') else params.get('community', '')
		if inv:
			mo_qs = mo_qs.filter(investor=inv)
		if regional:
			mo_qs = mo_qs.filter(regional_area_manager=regional)
		if community:
			mo_qs = mo_qs.filter(property_name=community)

		# period / bucket decision
		# Prefer service-provided selection; fall back to explicit month/quarter/year
		sel_period = svc_ctx.get('selected_period')
		if not sel_period:
			sel_period = svc_ctx.get('period_month') or svc_ctx.get('period_quarter') or svc_ctx.get('period_year')
		sel_mode = svc_ctx.get('period_mode') or (params.get('period_mode') if hasattr(params, 'get') else None)
		
		# Support multi-month for move-out reasons chart
		selected_months_mo = []
		if sel_mode == 'month':
			if hasattr(params, 'getlist'):
				selected_months_mo = params.getlist('period_month')
			if not selected_months_mo and sel_period:
				selected_months_mo = [sel_period]
		
		# Support multi-quarter for move-out reasons chart
		selected_quarters_mo = []
		if sel_mode == 'quarter':
			# Use request.GET directly to ensure we get all query parameters
			if hasattr(request.GET, 'getlist'):
				selected_quarters_mo = request.GET.getlist('period_quarter')
			elif hasattr(params, 'getlist'):
				selected_quarters_mo = params.getlist('period_quarter')
			if not selected_quarters_mo and sel_period:
				selected_quarters_mo = [sel_period]
		
		# Support multi-year for move-out reasons chart
		selected_years_mo = []
		if sel_mode == 'year':
			# Use request.GET directly to ensure we get all query parameters
			if hasattr(request.GET, 'getlist'):
				selected_years_mo = request.GET.getlist('period_year')
			elif hasattr(params, 'getlist'):
				selected_years_mo = params.getlist('period_year')
			if not selected_years_mo and sel_period:
				selected_years_mo = [sel_period]
		
		year, months = parse_period(sel_period) if sel_period else (None, None)
		import calendar as _calendar
		# Show yearly grouping only when period_year is 'all' or multiple years are selected
		# For a single specific year, show monthly breakdown
		show_by_year = (sel_mode == 'year' and (not selected_years_mo or len(selected_years_mo) > 1 or (selected_years_mo and selected_years_mo[0] == 'all')))

		# narrow when specific year/month selected
		if not show_by_year:
			if sel_mode == 'month' and selected_months_mo:
				# Multi-month support for move-out chart
				from django.db.models import Q
				q_filter = Q()
				for pm in selected_months_mo:
					mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', pm)
					if mm:
						month_name, yr = mm.groups()
						try:
							month_num = list(_calendar.month_abbr).index(month_name)
							q_filter |= Q(enddateofmonth__month=month_num, enddateofmonth__year=int(yr))
						except Exception:
							pass
				if q_filter:
					mo_qs = mo_qs.filter(q_filter)
			elif sel_mode == 'quarter' and selected_quarters_mo:
				# Multi-quarter support for move-out chart
				from django.db.models import Q
				q_filter = Q()
				for pq in selected_quarters_mo:
					parts = re.split('[-_]', pq)
					qpart = parts[0].upper()
					yr = parts[-1]
					qnum = int(re.sub('[^0-9]', '', qpart))
					start_month = (qnum - 1) * 3 + 1
					months_in_quarter = [start_month, start_month + 1, start_month + 2]
					for month_num in months_in_quarter:
						q_filter |= Q(enddateofmonth__month=month_num, enddateofmonth__year=int(yr))
				if q_filter:
					mo_qs = mo_qs.filter(q_filter)
			elif sel_mode == 'year' and selected_years_mo and len(selected_years_mo) == 1:
				# Single year selected: filter to that year and show monthly breakdown
				yr = selected_years_mo[0]
				if yr and yr != 'all':
					try:
						mo_qs = mo_qs.filter(enddateofmonth__year=int(yr))
					except Exception:
						pass
			elif year and months:
				month_nums = []
				for m in months:
					try:
						month_nums.append(list(_calendar.month_abbr).index(m))
					except Exception:
						pass
				if month_nums:
					mo_qs = mo_qs.filter(enddateofmonth__month__in=month_nums, enddateofmonth__year=int(year))
				else:
					mo_qs = mo_qs.filter(enddateofmonth__year=int(year))
			elif year and year != 'all':
				try:
					mo_qs = mo_qs.filter(enddateofmonth__year=int(year))
				except Exception:
					pass
		else:
			# show_by_year is True: filter by multiple years or 'all'
			if sel_mode == 'year' and selected_years_mo:
				# Multi-year support for move-out chart
				from django.db.models import Q
				q_filter = Q()
				for yr in selected_years_mo:
					if yr and yr != 'all':
						try:
							q_filter |= Q(enddateofmonth__year=int(yr))
						except Exception:
							pass
				if q_filter:
					mo_qs = mo_qs.filter(q_filter)

		# Pick top N categories overall to keep chart readable
		top_n = 6
		top_cats_qs = mo_qs.values('moveout_category').annotate(total=Sum('move_out_count')).order_by('-total')[:top_n]
		top_cats = [r.get('moveout_category') for r in list(top_cats_qs) if r.get('moveout_category')]

		# grouped single query: bucket + category -> sum
		if show_by_year:
			grouped = (mo_qs
					 .annotate(year=ExtractYear('enddateofmonth'))
					 .values('year', 'moveout_category')
					 .annotate(sum_count=Sum('move_out_count'))
					 .order_by('year'))
		else:
			grouped = (mo_qs
					 .annotate(year=ExtractYear('enddateofmonth'), month=ExtractMonth('enddateofmonth'))
					 .values('year', 'month', 'moveout_category')
					 .annotate(sum_count=Sum('move_out_count'))
					 .order_by('year', 'month'))

		rows = list(grouped)

		# build labels (last 6 buckets by default, or all if multiple periods selected) and initialize series map
		labels = []
		if show_by_year:
			years = sorted({int(r.get('year')) for r in rows if r.get('year') is not None})
			# When multiple years selected, show all; otherwise show last 6
			if selected_years_mo and len(selected_years_mo) > 1:
				labels = [str(y) for y in years]
			else:
				labels = [str(y) for y in years][-6:]
		else:
			from datetime import datetime as _dt
			month_keys = []
			for r in rows:
				y = int(r.get('year') or 0)
				m = int(r.get('month') or 0)
				if y and m:
					month_keys.append((y, m))
			month_keys = sorted(set(month_keys))
			# When multiple quarters/months selected, show all; otherwise show last 6
			if (selected_quarters_mo and len(selected_quarters_mo) > 1) or (selected_months_mo and len(selected_months_mo) > 1):
				labels = [ _dt(y, m, 1).strftime('%b-%y') for (y,m) in month_keys]
			else:
				labels = [ _dt(y, m, 1).strftime('%b-%y') for (y,m) in month_keys][-6:]

		# initialize series for top categories (if none found, use any categories present)
		if not top_cats:
			top_cats = sorted({r.get('moveout_category') for r in rows if r.get('moveout_category')})[:top_n]

		series_map = {cat: [0]*len(labels) for cat in top_cats}

		# fill series_map with sums from rows
		for r in rows:
			cat = r.get('moveout_category')
			if not cat or cat not in series_map:
				continue
			if show_by_year:
				key = str(int(r.get('year') or 0))
				if key in labels:
					idx = labels.index(key)
					series_map[cat][idx] = int(r.get('sum_count') or 0)
			else:
				y = int(r.get('year') or 0)
				m = int(r.get('month') or 0)
				try:
					key = _dt(y, m, 1).strftime('%b-%y')
				except Exception:
					continue
				if key in labels:
					idx = labels.index(key)
					series_map[cat][idx] = int(r.get('sum_count') or 0)

		# prepare Chart.js datasets
		palette = ['#1E40AF', '#059669', '#5B21B6', '#EA580C', '#7C2D12', '#0E555A']
		datasets = []
		for i, cat in enumerate(top_cats):
			datasets.append({'label': cat, 'data': series_map.get(cat, []), 'backgroundColor': palette[i % len(palette)]})

		chart_moveout = {'labels': labels, 'datasets': datasets}
	except Exception:
		# keep default empty chart_moveout if anything fails
		pass


	# Convert selected_month to JSON-safe format for template JS consumption
	# Use request.GET.getlist to ensure we get ALL selected months from URL
	selected_months_from_url = request.GET.getlist('period_month')
	if selected_months_from_url:
		selected_months_json = json.dumps(selected_months_from_url)
	else:
		selected_months_raw = svc_ctx.get('period_month')
		if isinstance(selected_months_raw, list):
			selected_months_json = json.dumps(selected_months_raw)
		elif selected_months_raw:
			selected_months_json = json.dumps([selected_months_raw])
		else:
			selected_months_json = json.dumps([])

	context = {
		'user': user,
		'properties': svc_ctx.get('properties_page'),
		'kpi': svc_ctx.get('kpi'),
	'period_options': svc_ctx.get('periods'),
	# Ensure templates see a concrete selected_period so client UI (filter pane)
	# can initialize correctly. Prefer svc_ctx.selected_period, otherwise
	# fall back to the explicit period_month/period_quarter/period_year values
	# that the PageService may set when defaulting to the latest month.
	'selected_period': (svc_ctx.get('selected_period') or svc_ctx.get('period_month') or svc_ctx.get('period_quarter') or svc_ctx.get('period_year')),
		'period_display_label': svc_ctx.get('period_display_label'),  # Comprehensive label for multi-period display
		'selected_year': svc_ctx.get('period_year'),
		'selected_years': svc_ctx.get('period_years', []),  # List of selected years
		'selected_quarter': svc_ctx.get('period_quarter'),
		'selected_quarters': svc_ctx.get('period_quarters', []),  # List of selected quarters
		'selected_month': svc_ctx.get('period_month'),
		'selected_month_json': selected_months_json,
		'selected_period_mode': svc_ctx.get('period_mode'),
		# Use filtered properties so the table reflects applied filters (period/investor/etc.)
		'all_properties': svc_ctx.get('properties_list'),
		# Conditionally exclude BLACKSTONE/LIVCOR from the investors dropdown for
		# periods after June 2025 unless the user explicitly selected that investor.
		'unfiltered_investors': svc_unfiltered_ctx.get('investors'),
		'investors': None,
		'regional_managers': svc_unfiltered_ctx.get('managers'),
		# Deduplicate communities list by property_name (when multi-period, same property appears multiple times)
		'communities': list({(p.get('property_name') or p.get('name') or p.get('community') or str(p)): p for p in (svc_ctx.get('properties_list') or [])}.values()),
		'inv_to_regional': svc_unfiltered_ctx.get('investor_managers'),
		'inv_reg_to_communities': inv_reg_to_communities,
		'inv_to_regional_json': json.dumps(svc_unfiltered_ctx.get('investor_managers', {})),
		'inv_reg_to_communities_json': json.dumps(inv_reg_to_communities),
		'chart_rent_json': json.dumps(svc_ctx.get('chart_rent', {})),
		'chart_occrev_json': json.dumps(svc_ctx.get('chart_occrev', {})),
		'chart_correlation_json': json.dumps(svc_ctx.get('chart_correlation', {})),
		'chart_renewals_json': json.dumps(chart_renewals),
		'chart_expense_json': json.dumps(chart_expense),
		'chart_moveout_json': json.dumps(chart_moveout),
	}

	# Apply KPI overrides computed by the consolidated monthly aggregation (if any)
	try:
		kpi = context.get('kpi') or {}
		for k, v in (kpi_overrides or {}).items():
			kpi[k] = v
		context['kpi'] = kpi
	except Exception:
		# keep existing KPIs on unexpected failures
		pass

	chart_props = dict(svc_ctx.get('chart_properties', {}) or {})

	context['chart_properties_json'] = json.dumps(chart_props)
	# Support multi-select: pass arrays instead of single values
	context['selected_investor'] = request.GET.getlist('investor') or [request.GET.get('investor', '')]
	context['selected_regional_manager'] = request.GET.getlist('regional_manager') or [request.GET.get('regional_manager', '')]
	context['selected_community'] = request.GET.getlist('community') or [request.GET.get('community', '')]
	context['selected_month'] = request.GET.getlist('period_month') or [request.GET.get('period_month', '')]
	# Filter out empty strings
	context['selected_investor'] = [i for i in context['selected_investor'] if i]
	context['selected_regional_manager'] = [rm for rm in context['selected_regional_manager'] if rm]
	context['selected_community'] = [c for c in context['selected_community'] if c]
	context['selected_month'] = [m for m in context['selected_month'] if m]

	# Build conditional investors list: start from unfiltered set provided by PageService
	try:
		_unfiltered_investors = context.pop('unfiltered_investors', svc_unfiltered_ctx.get('investors') or [])
		# Determine whether selected period is after June 2025 using same logic as earlier
		def _period_after_jun_2025_local(mode, year, months_list, quarter):
			import re, calendar
			try:
				if mode == 'month' and months_list:
					mval = months_list if isinstance(months_list, str) else (months_list[0] if months_list else '')
					mm = re.match(r'([A-Za-z]{3})[- ](\d{4})', mval)
					if mm:
						mon_abbr, yr = mm.groups()
						mon_num = list(calendar.month_abbr).index(mon_abbr)
						yr = int(yr)
						return (yr > 2025) or (yr == 2025 and mon_num > 6)
				if mode == 'quarter' and quarter:
					mm = re.search(r'Q(\d)', quarter, re.I)
					yy = re.search(r'(\d{4})', quarter)
					if mm and yy:
						qnum = int(mm.group(1))
						yr = int(yy.group(1))
						return (yr > 2025) or (yr == 2025 and qnum >= 3)
				if mode == 'year' and year and str(year).isdigit():
					return int(year) > 2025
			except Exception:
				return False
			return False

		sel_mode = svc_ctx.get('period_mode') or (params.get('period_mode') if hasattr(params, 'get') else None)
		sel_period = svc_ctx.get('selected_period') or svc_ctx.get('period_month') or svc_ctx.get('period_quarter') or svc_ctx.get('period_year')
		year, months = parse_period(sel_period) if sel_period else (None, None)
		exclude_blackstone = _period_after_jun_2025_local(sel_mode, year, sel_period, svc_ctx.get('period_quarter'))
		selected_inv_list = context.get('selected_investor', [])  # now an array
		# Business rule: when period is after Jun-2025, hide BLACKSTONE/LIVCOR unless
		# the user explicitly selected BLACKSTONE/LIVCOR. If the user selected other
		# investors but not BLACKSTONE, do not surface BLACKSTONE in the dropdown.
		if exclude_blackstone:
			# normalize selected list for comparison
			sel_up = [s.upper() for s in selected_inv_list]
			if 'BLACKSTONE/LIVCOR' in sel_up:
				# user explicitly selected BLACKSTONE/LIVCOR — preserve it and any missing selections
				missing_invs = [inv for inv in selected_inv_list if inv and inv not in _unfiltered_investors]
				if missing_invs:
					context['investors'] = _unfiltered_investors + missing_invs
				else:
					context['investors'] = _unfiltered_investors
			else:
				# period is after Jun-2025 and user did NOT explicitly choose BLACKSTONE — hide it
				context['investors'] = [i for i in _unfiltered_investors if i.upper() != 'BLACKSTONE/LIVCOR']
		else:
			# Not excluded by period — preserve the full list; ensure selected investors are present if explicitly chosen
			missing_invs = [inv for inv in selected_inv_list if inv and inv not in _unfiltered_investors]
			if missing_invs:
				# keep selected investors visible even if not in unfiltered (edge-case)
				context['investors'] = _unfiltered_investors + missing_invs
			else:
				context['investors'] = _unfiltered_investors
	except Exception:
		# On failure default to unmodified list
		context['investors'] = svc_unfiltered_ctx.get('investors')

	is_xhr = request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest' or request.headers.get('x-requested-with') == 'XMLHttpRequest'
	if is_xhr:
		# Check if requesting JSON format (for modal chart updates)
		if request.GET.get('format') == 'json':
			return JsonResponse({
				'chart_occrev_json': context.get('chart_occrev_json'),
				'chart_properties_json': context.get('chart_properties_json'),
				'chart_correlation_json': context.get('chart_correlation_json'),
				'chart_rent_json': context.get('chart_rent_json'),
				'selected_period': svc_ctx.get('selected_period')
			})
		# Default AJAX response for KPI/table updates
		kpi_html = render_to_string('dashboard/partials/_kpi_cards.html', context=context, request=request)
		table_html = render_to_string('dashboard/partials/_property_table.html', context=context, request=request)
		return JsonResponse({'kpi_html': kpi_html, 'table_html': table_html, 'selected_period': svc_ctx.get('selected_period')})

	return render(request, 'dashboard/dashboard.html', context)
