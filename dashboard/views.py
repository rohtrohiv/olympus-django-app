from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Max, Sum, Avg, Count, Q, F
from django.urls import reverse
from .models import OlympusLeaseTrendAnalysis, OlympusLeaseKpisTrendMonthly, FinanceKpiScorecard
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
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.template.loader import render_to_string
import re
from decimal import Decimal
import csv
from urllib.parse import urlencode


def sample_page(request):
	"""Simple page to test the shared base layout."""
	return render(request, 'sample.html', { 'page_title': 'Sample — Layout Test' })


@login_required
def financial_reporting(request):
	"""Financial reporting page with expandable P&L breakdown.
	
	Displays financial data by community with period comparison,
	expandable categories, and filtering capabilities.
	Fetches data from web_ai.card_drillthrough materialized view.
	"""
	from django.db.models import Sum, Count, Q, Max
	from dashboard.models import CardDrillthrough
	from datetime import datetime, timedelta
	from decimal import Decimal
	from dateutil.relativedelta import relativedelta
	
	# Get filter parameters from request (support multi-select)
	investor_filter = request.GET.getlist('investor') if request.GET.getlist('investor') else None
	regional_manager_filter = request.GET.getlist('regional_manager') if request.GET.getlist('regional_manager') else None
	community_filter = request.GET.getlist('community') if request.GET.getlist('community') else None
	
	# Default to Olympus investor if no filters applied and first visit
	if not investor_filter and not regional_manager_filter and not community_filter and not request.GET.get('period_month'):
		investor_filter = ['Olympus']
	
	# Period filtering - support multi-select for comparison
	period_mode = request.GET.get('period_mode', 'month')
	selected_months = request.GET.getlist('period_month') if request.GET.getlist('period_month') else []
	selected_years = request.GET.getlist('period_year') if request.GET.getlist('period_year') else []
	selected_quarters = request.GET.getlist('period_quarter') if request.GET.getlist('period_quarter') else []
	
	# Get latest available date from database (needed for month options)
	latest_date = CardDrillthrough.objects.aggregate(Max('month_end_date'))['month_end_date__max']
	
	# If no periods specified, default to one month before current
	if not selected_months and not selected_years and not selected_quarters:
		if latest_date:
			# Use one month before the CURRENT date (not the latest date in DB)
			current_date = datetime.now()
			default_date = current_date - relativedelta(months=1)
			selected_months = [default_date.strftime('%b-%Y')]
	
	# Parse selected periods into target dates for multi-period comparison
	target_dates = []
	period_labels = []
	
	if selected_years:
		# Years: e.g., "2025" -> expand to all 12 months
		for year_str in selected_years:
			try:
				target_year = int(year_str)
				for month in range(1, 13):
					target_dates.append({
						'year': target_year,
						'month': month,
						'label': year_str,
						'is_aggregate': True
					})
				if year_str not in period_labels:
					period_labels.append(year_str)
			except Exception:
				pass
	elif selected_quarters:
		# Quarters: e.g., "Q1-2025" -> expand to all months in Q1
		for quarter_str in selected_quarters:
			try:
				match = re.match(r'Q(\d)-(\d{4})', quarter_str)
				if match:
					quarter_num, year = match.groups()
					target_year = int(year)
					quarter_months = {
						'1': [1, 2, 3],
						'2': [4, 5, 6],
						'3': [7, 8, 9],
						'4': [10, 11, 12]
					}
					for month in quarter_months[quarter_num]:
						target_dates.append({
							'year': target_year,
							'month': month,
							'label': quarter_str,
							'is_aggregate': True
						})
					if quarter_str not in period_labels:
						period_labels.append(quarter_str)
			except Exception:
				pass
	elif selected_months:
		# Individual months
		for month_str in selected_months:
			try:
				match = re.match(r'([A-Za-z]{3})-(\d{4})', month_str)
				if match:
					month_abbr, year = match.groups()
					target_month = datetime.strptime(month_abbr, '%b').month
					target_year = int(year)
					target_dates.append({
						'year': target_year,
						'month': target_month,
						'label': month_str,
						'is_aggregate': False
					})
					if month_str not in period_labels:
						period_labels.append(month_str)
			except Exception:
				pass
	
	# Reverse period_labels to show chronologically (earliest first)
	# This way Oct-2024 comes before Nov-2024 before Dec-2024
	period_labels.reverse()
	target_dates.reverse()
	
	# Calculate previous period for first period variance comparison
	# Only fetch if it's not already in the selected periods
	previous_period_info = None
	if target_dates and period_labels:
		first_target = target_dates[0]
		first_date = datetime(first_target['year'], first_target['month'], 1)
		prev_date = first_date - relativedelta(months=1)
		prev_label = prev_date.strftime('%b-%Y')
		
		# Only fetch previous period if it's not already selected
		if prev_label not in period_labels:
			previous_period_info = {
				'year': prev_date.year,
				'month': prev_date.month,
				'label': f"_prev_{prev_label}"  # Internal label
			}
	
	# Build optimized query with select_related to reduce database hits
	queryset = CardDrillthrough.objects.all()
	
	# Apply period filter (support multiple periods with OR, plus previous period for variance)
	if target_dates:
		period_queries = []
		for target_date in target_dates:
			period_queries.append(
				Q(month_end_date__year=target_date['year'], month_end_date__month=target_date['month'])
			)
		# Add previous period for variance calculation
		if previous_period_info:
			period_queries.append(
				Q(month_end_date__year=previous_period_info['year'], month_end_date__month=previous_period_info['month'])
			)
		# Combine with OR
		if period_queries:
			period_q = period_queries[0]
			for pq in period_queries[1:]:
				period_q |= pq
			queryset = queryset.filter(period_q)
	
	# Apply other filters
	if investor_filter:
		queryset = queryset.filter(investor__in=investor_filter)
	if regional_manager_filter:
		queryset = queryset.filter(regional_area_manager__in=regional_manager_filter)
	if community_filter:
		queryset = queryset.filter(property_name__in=community_filter)
	
	# Fetch all data in one query - optimized for production
	# Using values() is much faster than model instances
	all_records = list(queryset.values(
		'community', 'property_name', 'total_units', 'month_end_date',
		'category_name', 'parent_category_name', 'sub_category_name', 'sub_sub_category_name',
		'income_values', 'income_values_per_unit_all'
	))
	
	# Group by community and period in Python
	community_map = {}
	
	for record in all_records:
		community_key = record['community']
		if not community_key:
			continue
		
		# Determine period label for this record
		record_date = record['month_end_date']
		record_period = None
		
		# Check if this is one of the target dates
		for target_date in target_dates:
			if record_date.year == target_date['year'] and record_date.month == target_date['month']:
				record_period = target_date['label']
				break
		
		# Check if this is the previous period for variance calculation
		if not record_period and previous_period_info:
			if record_date.year == previous_period_info['year'] and record_date.month == previous_period_info['month']:
				record_period = previous_period_info['label']
		
		if not record_period:
			continue
		
		# Initialize community if not exists
		if community_key not in community_map:
			community_map[community_key] = {
				'name': record['property_name'] or community_key,
				'units': record['total_units'] or 1,
				'periods': {}
			}
		
		# Initialize period if not exists
		if record_period not in community_map[community_key]['periods']:
			community_map[community_key]['periods'][record_period] = {
				'total_actual': Decimal('0'),
				'breakdown': {}
			}
		
		# Parse income values
		income_val = record['income_values']
		if income_val is not None:
			if isinstance(income_val, str):
				income_val = Decimal(income_val.replace('$', '').replace(',', '').replace('(', '-').replace(')', ''))
			else:
				income_val = Decimal(str(income_val))
		else:
			income_val = Decimal('0')
		
		per_unit_val = record['income_values_per_unit_all']
		if per_unit_val is not None:
			if isinstance(per_unit_val, str):
				per_unit_val = Decimal(per_unit_val.replace('$', '').replace(',', '').replace('(', '-').replace(')', ''))
			else:
				per_unit_val = Decimal(str(per_unit_val))
		else:
			per_unit_val = Decimal('0')
		
		# Add to period total
		community_map[community_key]['periods'][record_period]['total_actual'] += income_val
		
		# Build hierarchical structure: Parent > Sub > SubSub
		parent = record['parent_category_name'] or 'Other'
		sub = record['sub_category_name'] or 'Uncategorized'
		sub_sub = record['sub_sub_category_name'] or record['category_name'] or 'Other'
		
		# Skip Capital Expenditures and certain categories
		if parent in ['Capital Expenditures', 'Net Income', 'Net Operating Income']:
			continue
		
		# Initialize parent category if not exists
		if parent not in community_map[community_key]['periods'][record_period]['breakdown']:
			community_map[community_key]['periods'][record_period]['breakdown'][parent] = {
				'actual': Decimal('0'),
				'sub_categories': {}
			}
		
		# Add to parent total
		community_map[community_key]['periods'][record_period]['breakdown'][parent]['actual'] += income_val
		
		# Initialize sub category if not exists
		if sub not in community_map[community_key]['periods'][record_period]['breakdown'][parent]['sub_categories']:
			community_map[community_key]['periods'][record_period]['breakdown'][parent]['sub_categories'][sub] = {
				'actual': Decimal('0'),
				'items': []
			}
		
		# Add to sub category total
		community_map[community_key]['periods'][record_period]['breakdown'][parent]['sub_categories'][sub]['actual'] += income_val
		
		# Add line item
		community_map[community_key]['periods'][record_period]['breakdown'][parent]['sub_categories'][sub]['items'].append({
			'name': sub_sub,
			'actual': float(income_val),
			'variance': 0.0,
			'per_unit': float(per_unit_val) if per_unit_val else (float(income_val) / community_map[community_key]['units'] if community_map[community_key]['units'] else 0)
		})
	
	# Convert to list with multi-period structure
	community_data = []
	for comm_key, comm_data in community_map.items():
		# Build periods data first without variance
		periods_data = {}
		for period_label in period_labels:
			if period_label in comm_data['periods']:
				period_info = comm_data['periods'][period_label]
				per_unit_total = float(period_info['total_actual']) / comm_data['units'] if comm_data['units'] else 0
				
				# Convert breakdown from dict to sorted list
				breakdown_list = []
				for parent_name, parent_data in sorted(period_info['breakdown'].items()):
					parent_total = float(parent_data['actual'])
					parent_per_unit = parent_total / comm_data['units'] if comm_data['units'] else 0
					
					# Build sub-categories
					sub_categories_list = []
					for sub_name, sub_data in sorted(parent_data['sub_categories'].items()):
						sub_total = float(sub_data['actual'])
						sub_per_unit = sub_total / comm_data['units'] if comm_data['units'] else 0
						
						# Build items with initial variance 0
						items_list = []
						for item in sub_data['items']:
							items_list.append({
								'name': item['name'],
								'actual': item['actual'],
								'per_unit': item['per_unit'],
								'variance': 0.0
							})
						
						sub_categories_list.append({
							'name': sub_name,
							'actual': sub_total,
							'per_unit': sub_per_unit,
							'variance': 0.0,
							'items': sorted(items_list, key=lambda x: abs(x['actual']), reverse=True)[:10]
						})
					
					breakdown_list.append({
						'parent': parent_name,
						'actual': parent_total,
						'per_unit': parent_per_unit,
						'variance': 0.0,
						'sub_categories': sub_categories_list
					})
				
				periods_data[period_label] = {
					'actual': float(period_info['total_actual']),
					'variance': 0.0,
					'per_unit': per_unit_total,
					'breakdown': breakdown_list
				}
			else:
				# No data for this period
				periods_data[period_label] = {
					'actual': 0.0,
					'variance': 0.0,
					'per_unit': 0.0,
					'breakdown': []
				}
		
		# Calculate variance for each period (compare to previous period in the list or DB previous)
		for i in range(len(period_labels)):
			current_period = period_labels[i]
			previous_period = None
			use_raw_prev_data = False
			
			if i > 0:
				# Use previous period in the selected list
				previous_period = period_labels[i - 1]
			elif i == 0 and previous_period_info:
				# For first period, use the period immediately before it from DB
				previous_period = previous_period_info['label']
				use_raw_prev_data = True
			
			if previous_period:
				# Calculate top-level variance
				current_actual = periods_data[current_period]['actual']
				previous_actual = 0
				
				# Get previous period data
				if use_raw_prev_data and previous_period in comm_data['periods']:
					# Use raw data from community_map for DB-fetched previous period
					prev_period_info = comm_data['periods'][previous_period]
					previous_actual = float(prev_period_info['total_actual'])
				elif previous_period in periods_data:
					# Use data from periods_data for selected periods
					previous_actual = periods_data[previous_period]['actual']
				
				if previous_actual != 0:
					periods_data[current_period]['variance'] = round(((current_actual - previous_actual) / abs(previous_actual)) * 100, 1)
				else:
					periods_data[current_period]['variance'] = 0.0
				
				# Calculate breakdown variances
				current_breakdown = {item['parent']: item for item in periods_data[current_period]['breakdown']}
				previous_breakdown = {}
				
				# Get previous breakdown
				if use_raw_prev_data and previous_period in comm_data['periods']:
					# Build previous breakdown from raw data for DB-fetched previous period
					prev_period_raw = comm_data['periods'][previous_period]
					for parent_name, parent_data in prev_period_raw['breakdown'].items():
						parent_total = float(parent_data['actual'])
						parent_per_unit = parent_total / comm_data['units'] if comm_data['units'] else 0
						
						sub_categories_dict = {}
						for sub_name, sub_data in parent_data['sub_categories'].items():
							sub_total = float(sub_data['actual'])
							sub_per_unit = sub_total / comm_data['units'] if comm_data['units'] else 0
							
							items_dict = {}
							for item in sub_data['items']:
								items_dict[item['name']] = {
									'actual': item['actual'],
									'per_unit': item['per_unit']
								}
							
							sub_categories_dict[sub_name] = {
								'actual': sub_total,
								'per_unit': sub_per_unit,
								'items': items_dict
							}
						
						previous_breakdown[parent_name] = {
							'actual': parent_total,
							'per_unit': parent_per_unit,
							'sub_categories': sub_categories_dict
						}
				elif previous_period in periods_data:
					# Use data from periods_data for selected periods
					previous_breakdown = {item['parent']: item for item in periods_data[previous_period]['breakdown']}
				
				for parent_name, parent_data in current_breakdown.items():
					if parent_name in previous_breakdown:
						prev_parent = previous_breakdown[parent_name]
						if prev_parent['actual'] != 0:
							parent_data['variance'] = round(((parent_data['actual'] - prev_parent['actual']) / abs(prev_parent['actual'])) * 100, 1)
						
						# Calculate sub-category variances
						current_subs = {sub['name']: sub for sub in parent_data['sub_categories']}
						
						# Handle previous_subs as either dict or list
						if isinstance(prev_parent['sub_categories'], dict):
							previous_subs = prev_parent['sub_categories']
						else:
							previous_subs = {sub['name']: sub for sub in prev_parent['sub_categories']}
						
						for sub_name, sub_data in current_subs.items():
							if sub_name in previous_subs:
								prev_sub = previous_subs[sub_name]
								if prev_sub['actual'] != 0:
									sub_data['variance'] = round(((sub_data['actual'] - prev_sub['actual']) / abs(prev_sub['actual'])) * 100, 1)
								
								# Calculate item variances
								current_items = {item['name']: item for item in sub_data['items']}
								
								# Handle previous items as either dict or list
								if isinstance(prev_sub.get('items', {}), dict):
									previous_items = prev_sub['items']
								else:
									previous_items = {item['name']: item for item in prev_sub.get('items', [])}
								
								for item_name, item_data in current_items.items():
									if item_name in previous_items:
										prev_item = previous_items[item_name]
										prev_item_actual = prev_item.get('actual', prev_item) if isinstance(prev_item, dict) else prev_item
										if isinstance(prev_item_actual, dict):
											prev_item_actual = prev_item_actual.get('actual', 0)
										if prev_item_actual != 0:
											item_data['variance'] = round(((item_data['actual'] - prev_item_actual) / abs(prev_item_actual)) * 100, 1)
		
		community_data.append({
			'name': comm_data['name'],
			'units': comm_data['units'],
			'periods': periods_data
		})
	
	# Sort by first period's actual amount descending
	if period_labels:
		community_data.sort(key=lambda x: abs(x['periods'][period_labels[0]]['actual']), reverse=True)
	
	# Get filter options - Optimize with caching and limits
	# Cache dropdown options for 1 hour to reduce database load
	if not investor_filter:
		cache_key = 'financial_reporting_investors'
		investors = cache.get(cache_key)
		if investors is None:
			investors = list(CardDrillthrough.objects.values_list('investor', flat=True).distinct().order_by('investor')[:50])
			investors = [inv for inv in investors if inv]
			cache.set(cache_key, investors, 3600)  # Cache for 1 hour
	else:
		investors = investor_filter
	
	if not regional_manager_filter:
		cache_key = 'financial_reporting_managers'
		managers = cache.get(cache_key)
		if managers is None:
			managers = list(CardDrillthrough.objects.values_list('regional_area_manager', flat=True).distinct().order_by('regional_area_manager')[:50])
			managers = [mgr for mgr in managers if mgr]
			cache.set(cache_key, managers, 3600)
	else:
		managers = regional_manager_filter
	
	if not community_filter:
		cache_key = 'financial_reporting_communities'
		communities_list = cache.get(cache_key)
		if communities_list is None:
			communities_list = list(CardDrillthrough.objects.values_list('property_name', flat=True).distinct().order_by('property_name')[:100])
			communities_list = [comm for comm in communities_list if comm]
			cache.set(cache_key, communities_list, 3600)
	else:
		communities_list = community_filter
	
	# Build month options for dropdown - fetch actual dates from database with caching
	cache_key = 'financial_reporting_month_options'
	month_options = cache.get(cache_key)
	if month_options is None:
		distinct_dates = CardDrillthrough.objects.dates('month_end_date', 'month', order='DESC')
		month_options = [date.strftime('%b-%Y') for date in distinct_dates]
		cache.set(cache_key, month_options, 3600)  # Cache for 1 hour
	
	# Build period_options structure for year/quarter dropdowns - fetch actual years from database with caching
	cache_key = 'financial_reporting_period_options'
	period_options = cache.get(cache_key)
	if period_options is None:
		distinct_years = CardDrillthrough.objects.dates('month_end_date', 'year', order='DESC')
		period_options = {}
		for year_date in distinct_years:
			year = year_date.year
			period_options[str(year)] = {
				'quarters': {
					'Q1': ['Jan', 'Feb', 'Mar'],
					'Q2': ['Apr', 'May', 'Jun'],
					'Q3': ['Jul', 'Aug', 'Sep'],
					'Q4': ['Oct', 'Nov', 'Dec']
				}
			}
		cache.set(cache_key, period_options, 3600)  # Cache for 1 hour
	
	context = {
		'page_title': 'Financial Reporting',
		'communities': community_data,  # Show all communities
		'total_communities': len(community_data),  # Total count for display
		'period_options': period_options,  # For year/quarter dropdowns
		'month_options': month_options,  # Pre-formatted month options
		'investors': investors,
		'regional_managers': managers,
		'all_communities': communities_list,
		'selected_investors': investor_filter or [],  # Multi-select
		'selected_regional_managers': regional_manager_filter or [],  # Multi-select
		'selected_communities': community_filter or [],  # Multi-select
		'selected_months': selected_months,  # Multi-select months
		'selected_years': selected_years,  # Multi-select years
		'selected_quarters': selected_quarters,  # Multi-select quarters
		'period_mode': period_mode,
		'total_communities': len(community_data),
		'period_labels': period_labels,  # List of period labels for table headers (e.g., ['Q1-2025', 'Q2-2025'])
	}
	
	return render(request, 'dashboard/financial_reporting.html', context)


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
	
	# Get period options for filter panel (years with quarters and months)
	# Use DashboardPageService to get properly formatted period options
	from .dashboard_service import DashboardPageService
	# Pass empty params to get all available periods
	period_service = DashboardPageService({})
	period_context = period_service.get_context()
	
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
		# Filter panel required context
		'period_options': period_context.get('periods'),
		'selected_year': period_context.get('period_year'),
		'selected_quarter': period_context.get('period_quarter'),
		'selected_month': period_context.get('period_month'),
		'selected_period_mode': period_context.get('period_mode', 'year'),
		'investors_list': investors,  # For filter panel dropdown
		'regional_managers': managers,  # For filter panel dropdown
		'communities': [{'property_name': p} for p in properties],  # For filter panel dropdown
		'inv_to_regional_json': json.dumps(investor_managers),
		'inv_reg_to_communities_json': json.dumps(investor_properties),
	}
	return render(request, 'property_analytics_advanced.html', context)


@login_required 
def analytics_query(request):
	"""API endpoint for advanced analytics queries.
	
	Processes filter parameters and returns aggregated data for charts,
	tables, and summary statistics. Supports various groupings and metrics.
	Accepts filters from both the filter panel and inline form controls.
	"""
	if request.method != 'POST':
		return JsonResponse({'error': 'POST method required'}, status=405)
	
	try:
		# Debug logging
		import logging
		logger = logging.getLogger(__name__)
		logger.info(f"Analytics query received - POST data: {dict(request.POST.lists())}")
		
		# Get filter parameters - support both filter panel and inline form
		# Filter panel parameters
		period_years = request.POST.getlist('period_year')
		period_quarters = request.POST.getlist('period_quarter')
		period_months = request.POST.getlist('period_month')
		period_mode = request.POST.get('period_mode', '')
		filter_investors = request.POST.getlist('investor')  # from filter panel
		filter_managers = request.POST.getlist('regional_manager')  # from filter panel
		filter_communities = request.POST.getlist('community')  # from filter panel
		
		logger.info(f"Period mode: {period_mode}, Years: {period_years}, Quarters: {period_quarters}, Months: {period_months}")
		logger.info(f"Filters - Investors: {filter_investors}, Managers: {filter_managers}, Communities: {filter_communities}")
		
		# Inline form parameters (legacy)
		date_range = request.POST.get('date_range', 'last_30_days')
		start_date = request.POST.get('start_date')
		end_date = request.POST.get('end_date') 
		properties = request.POST.getlist('properties')
		investors = request.POST.getlist('investors')
		managers = request.POST.getlist('managers')
		
		# Merge filter panel and inline parameters
		if filter_investors:
			investors = filter_investors
		if filter_managers:
			managers = filter_managers
		if filter_communities:
			properties = filter_communities
			
		group_by = request.POST.get('group_by', '')
		metrics = request.POST.getlist('metrics')
		aggregation = request.POST.get('aggregation', 'sum')
		
		# Build base queryset
		qs = OlympusLeaseTrendAnalysis.objects.all()
		
		# Apply period filters from filter panel if present
		if period_mode and (period_years or period_quarters or period_months):
			logger.info(f"Applying period filters - Mode: {period_mode}")
			from datetime import datetime, timedelta
			if period_mode == 'year' and period_years:
				if 'all' not in period_years:
					# Year filtering: snapshotdate is typically stored as "Mon-YYYY" format
					# Build Q objects for each year to match any month in that year
					year_queries = Q()
					for year in period_years:
						if year and year != 'all':
							# Match any date containing the year (e.g., "Jan-2024", "Feb-2024", etc.)
							year_queries |= Q(snapshotdate__contains=f'-{year}')
							logger.info(f"Adding year filter for: {year}")
					
					if year_queries:
						qs = qs.filter(year_queries)
						logger.info(f"Queryset filtered by years, count: {qs.count()}")
			elif period_mode == 'quarter' and period_quarters:
				# Quarter filtering: Parse quarter format like "Q1-2024" or "2024-Q1"
				if 'all' not in period_quarters:
					quarter_queries = Q()
					for quarter in period_quarters:
						if quarter and quarter != 'all':
							# Parse quarter (e.g., "Q1-2024" or "2024-Q1")
							if '-Q' in quarter or 'Q' in quarter:
								parts = quarter.replace('Q', '').split('-')
								if len(parts) == 2:
									q_num = parts[0] if parts[0].isdigit() and int(parts[0]) <= 4 else parts[1]
									year = parts[1] if parts[0].isdigit() and int(parts[0]) <= 4 else parts[0]
									
									# Map quarter to months
									quarter_months = {
										'1': ['Jan', 'Feb', 'Mar'],
										'2': ['Apr', 'May', 'Jun'],
										'3': ['Jul', 'Aug', 'Sep'],
										'4': ['Oct', 'Nov', 'Dec']
									}
									
									months = quarter_months.get(str(q_num), [])
									for month in months:
										quarter_queries |= Q(snapshotdate__startswith=f'{month}-{year}')
					
					if quarter_queries:
						qs = qs.filter(quarter_queries)
			elif period_mode == 'month' and period_months:
				if 'all' not in period_months:
					# Period months format: "Oct-2025", exact match
					qs = qs.filter(snapshotdate__in=period_months)
		# Apply date filters from inline form if no period filters
		elif date_range:
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

				# Period filtering from filter panel (takes priority)
				snapshot_str = str(item.get('snapshotdate', ''))
				period_match = True
				
				if period_mode and (period_years or period_quarters or period_months):
					if period_mode == 'year' and period_years and 'all' not in period_years:
						# Check if snapshot contains any selected year (e.g., "Jan-2024" contains "2024")
						period_match = any(f'-{year}' in snapshot_str for year in period_years if year)
					elif period_mode == 'quarter' and period_quarters and 'all' not in period_quarters:
						# Check if snapshot month is in selected quarters
						quarter_match = False
						for quarter in period_quarters:
							if quarter and quarter != 'all':
								# Parse quarter (e.g., "Q1-2024")
								if '-Q' in quarter or 'Q' in quarter:
									parts = quarter.replace('Q', '').split('-')
									if len(parts) == 2:
										q_num = parts[0] if parts[0].isdigit() and int(parts[0]) <= 4 else parts[1]
										year = parts[1] if parts[0].isdigit() and int(parts[0]) <= 4 else parts[0]
										
										# Map quarter to months
										quarter_months = {
											'1': ['Jan', 'Feb', 'Mar'],
											'2': ['Apr', 'May', 'Jun'],
											'3': ['Jul', 'Aug', 'Sep'],
											'4': ['Oct', 'Nov', 'Dec']
										}
										
										months = quarter_months.get(str(q_num), [])
										for month in months:
											if snapshot_str.startswith(f'{month}-{year}'):
												quarter_match = True
												break
						period_match = quarter_match
					elif period_mode == 'month' and period_months and 'all' not in period_months:
						# Exact match for months (e.g., "Oct-2025")
						period_match = snapshot_str in period_months
				# Date range filtering (only if no period filters)
				elif date_range:
					sd = _to_date_generic(item.get('snapshotdate'))
					if sd is None:
						period_match = False
					else:
						if date_range == 'last_30_days' and sd < thirty:
							period_match = False
						elif date_range == 'last_90_days' and sd < ninety:
							period_match = False
						elif date_range == 'last_6_months' and sd < six_months:
							period_match = False
						elif date_range == 'last_year' and sd < one_year:
							period_match = False
						elif date_range == 'custom' and start_date and end_date:
							# parse provided start/end
							try:
								start_d = datetime.strptime(start_date, '%Y-%m-%d').date()
								ex_d = datetime.strptime(end_date, '%Y-%m-%d').date()
								if sd < start_d or sd > ex_d:
									period_match = False
							except Exception:
								pass
				
				if not period_match:
					continue

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


def _extract_filter_list(request, params, key):
	"""Return list of values for a multi-select filter key from request/params."""
	values = []
	if request is not None and hasattr(request, 'GET') and hasattr(request.GET, 'getlist'):
		values = request.GET.getlist(key)
	if not values:
		if hasattr(params, 'getlist'):
			values = params.getlist(key)
		else:
			param_val = params.get(key) if hasattr(params, 'get') else None
			if isinstance(param_val, (list, tuple)):
				values = list(param_val)
			elif param_val:
				values = [param_val]
	return [v for v in values if v]


def _month_label_to_date(label):
	if not label:
		return None
	for fmt in ('%b-%Y', '%Y-%m'):
		try:
			dt = datetime.strptime(label, fmt)
			return date(dt.year, dt.month, 1)
		except Exception:
			continue
	return None


def _quarter_label_to_months(label):
	if not label:
		return []
	label_norm = label.replace(' ', '').upper()
	match = re.match(r'Q([1-4])-(\d{4})', label_norm)
	if not match:
		match = re.match(r'(\d{4})-Q([1-4])', label_norm)
	if not match:
		return []
	if label_norm.startswith('Q'):
		q = int(match.group(1))
		year = int(match.group(2))
	else:
		year = int(match.group(1))
		q = int(match.group(2))
	start_month = (q - 1) * 3 + 1
	return [date(year, start_month + offset, 1) for offset in range(3)]


def _resolve_selected_month_starts(request, params, svc_ctx):
	"""Return list of month-start dates matching the current period selection."""
	sel_mode = svc_ctx.get('period_mode') or (params.get('period_mode') if hasattr(params, 'get') else None)
	months = []
	if sel_mode == 'month':
		labels = _extract_filter_list(request, params, 'period_month')
		if not labels:
			fallback = svc_ctx.get('selected_period') or svc_ctx.get('period_month')
			if fallback:
				labels = [fallback]
		for lbl in labels:
			dt = _month_label_to_date(lbl)
			if dt:
				months.append(dt)
	elif sel_mode == 'quarter':
		labels = _extract_filter_list(request, params, 'period_quarter')
		if not labels:
			fallback = svc_ctx.get('period_quarter') or svc_ctx.get('selected_period')
			if fallback:
				labels = [fallback]
		for lbl in labels:
			months.extend(_quarter_label_to_months(lbl))
	elif sel_mode == 'year':
		labels = _extract_filter_list(request, params, 'period_year')
		if not labels:
			fallback = svc_ctx.get('period_year') or svc_ctx.get('selected_period')
			if fallback:
				labels = [fallback]
		normalized = [l for l in labels if l]
		if normalized and not any(str(l).lower() == 'all' for l in normalized):
			for lbl in normalized:
				try:
					yr = int(lbl)
					for m in range(1, 13):
						months.append(date(yr, m, 1))
				except Exception:
					continue
	else:
		fallback = svc_ctx.get('selected_period')
		dt = _month_label_to_date(fallback)
		if dt:
			months.append(dt)
	return sorted({m for m in months})


def _quote_ident(identifier):
	ident = identifier.replace('"', '""').replace('%', '%%')
	return '"' + ident + '"'


def _get_finance_scorecard_columns():
	columns = []
	try:
		with connection.cursor() as cur:
			cur.execute(
				"""
				SELECT column_name
				FROM information_schema.columns
				WHERE table_schema = %s AND table_name = %s
				ORDER BY ordinal_position
				""",
				['web_ai', 'finance_kpi_scorecard']
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Finance KPI scorecard column introspection failed:', exc)

	if columns:
		return columns

	try:
		with connection.cursor() as cur:
			cur.execute(
				"""
				SELECT attname
				FROM pg_attribute
				WHERE attrelid = 'web_ai.finance_kpi_scorecard'::regclass
				  AND attnum > 0
				  AND NOT attisdropped
				ORDER BY attnum
				"""
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Finance KPI scorecard pg_attribute introspection failed:', exc)
	return columns


_KEYWORD_SYNONYMS = {
	'operating': ['operating', 'oper', 'op'],
	'revenue': ['revenue', 'rev'],
	'expense': ['expense', 'exp'],
	'yoy': ['yoy', 'yo_y', 'yy', 'yearover', 'year_over_year', 'year-over-year'],
	'executed': ['executed', 'exec'],
	'rent': ['rent'],
	'noi': ['noi'],
	'place': ['place', 'inplace', 'in_place'],
	'sq': ['sq', 'sqft', 'psf', 'per_sq', 'per_sqft'],
}


def _keyword_matches_column(name, keyword):
	options = _KEYWORD_SYNONYMS.get(keyword, [keyword])
	return any(opt in name for opt in options)


def _find_column_by_keywords(columns, keywords):
	for col in columns:
		name = (col or '').lower()
		if all(_keyword_matches_column(name, keyword) for keyword in keywords):
			return col
	return None


def _find_exact_column(columns, target):
	for col in columns:
		if col and col.lower() == target.lower():
			return col
	return None


TOTAL_UNIT_DRILL_VIEW = 'web_ai.total_unit_occupancy_drill_through'
TOTAL_UNIT_DRILL_PAGE_SIZE = 500

TOTAL_UNIT_FILTER_FIELDS = [
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'keywords': ['regional', 'vp']},
	{'key': 'regional_manager', 'label': 'Regional Manager', 'keywords': ['regional', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
	{'key': 'unit', 'label': 'Unit', 'keywords': ['unit']},
]

TOTAL_UNIT_TABLE_FIELDS = [
	{'key': 'property_name', 'label': 'Property Name', 'keywords': ['property', 'name']},
	{'key': 'unit_identifier', 'label': 'Unit', 'keywords': ['unit']},
	{'key': 'floor_plan', 'label': 'Floor Plan', 'keywords': ['floor', 'plan']},
	{'key': 'beds_baths', 'label': 'Beds / Baths', 'keywords': ['bed', 'bath']},
	{'key': 'amenity_value', 'label': 'Amenity Value', 'keywords': ['amenity', 'value']},
	{'key': 'move_in_date', 'label': 'Move In', 'keywords': ['move', 'in']},
	{'key': 'lease_start', 'label': 'Lease Start', 'keywords': ['lease', 'start']},
	{'key': 'lease_end', 'label': 'Lease End', 'keywords': ['lease', 'end']},
	{'key': 'lease_term', 'label': 'Lease Term', 'keywords': ['lease', 'term']},
	{'key': 'resident_name', 'label': 'Resident Name', 'keywords': ['resident', 'name']},
	{'key': 'effective_rent', 'label': 'Effective Rent', 'keywords': ['effective', 'rent']},
	{'key': 'market_rent', 'label': 'Market Rent', 'keywords': ['market', 'rent']},
	{'key': 'site_unit_id', 'label': 'Site / Unit Id', 'keywords': ['site', 'unit', 'id']},
]

TOTAL_UNIT_METRIC_HINTS = {
	'resident_name': ['resident', 'name'],
	'ntv_flag': ['ntv'],
	'exposure_8_weeks': ['exposure', '8'],
	'unit_type': ['type'],
	'move_out_date': ['move', 'out'],
	'scheduled_move_in': ['move', 'in'],
}

VACANT_NOT_LEASED_STATUSES = (
	'Vacant Not Leased Not Ready',
	'Vacant Not Leased Ready',
)
VACANT_LEASED_STATUSES = (
	'Vacant Leased Ready',
	'Vacant Leased Not Ready',
)
NTV_NOT_LEASED_STATUS = 'NTV Not Leased'
NTV_LEASED_STATUS = 'NTV Leased'
VACANT_NOT_LEASED_TYPES = (
	'Vacant Not Leased Not Ready',
	'Vacant Not Leased Ready',
)
VACANT_LEASED_TYPES = (
	'Vacant Leased Ready',
	'Vacant Leased Not Ready',
)
NTV_TYPES = (
	NTV_LEASED_STATUS,
	NTV_NOT_LEASED_STATUS,
)

TOTAL_UNIT_CURRENCY_FIELDS = {'amenity_value', 'effective_rent', 'market_rent'}
TOTAL_UNIT_DATE_FIELDS = {'move_in_date', 'lease_start', 'lease_end'}


def _clean_numeric_expr(column_name):
	ident = _quote_ident(column_name)
	return f"NULLIF(REGEXP_REPLACE({ident}::text, '[^0-9.-]', '', 'g'), '')::numeric"


def _get_total_unit_drill_columns():
	columns = []
	try:
		with connection.cursor() as cur:
			cur.execute(
				"""
				SELECT column_name
				FROM information_schema.columns
				WHERE table_schema = %s AND table_name = %s
				ORDER BY ordinal_position
				""",
				['web_ai', 'total_unit_occupancy_drill_through']
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Total unit drill-through column introspection failed:', exc)

	if columns:
		return columns

	try:
		with connection.cursor() as cur:
			cur.execute(
				"""
				SELECT attname
				FROM pg_attribute
				WHERE attrelid = 'web_ai.total_unit_occupancy_drill_through'::regclass
				  AND attnum > 0
				  AND NOT attisdropped
				ORDER BY attnum
				"""
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Total unit drill-through pg_attribute introspection failed:', exc)
	return columns


def _format_total_unit_value(alias, value):
	if value is None:
		return '--'
	if isinstance(value, (datetime, date)):
		return value.strftime('%b %d, %Y')
	if isinstance(value, Decimal):
		value = float(value)
	if alias in TOTAL_UNIT_CURRENCY_FIELDS:
		try:
			return f"${float(value):,.2f}"
		except Exception:
			return f"${value}"
	if alias in TOTAL_UNIT_DATE_FIELDS:
		try:
			parsed = datetime.strptime(str(value), '%Y-%m-%d').date()
			return parsed.strftime('%b %d, %Y')
		except Exception:
			return str(value)
	text = str(value).strip()
	return text or '--'


def _fetch_total_unit_filter_options(columns):
	options = {}
	for field in TOTAL_UNIT_FILTER_FIELDS:
		col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			options[field['key']] = []
			continue
		ident = _quote_ident(col)
		sql = (
			f"SELECT DISTINCT {ident} FROM {TOTAL_UNIT_DRILL_VIEW} "
			f"WHERE {ident} IS NOT NULL AND TRIM({ident}::text) <> '' "
			f"ORDER BY {ident} ASC LIMIT 400"
		)
		try:
			with connection.cursor() as cur:
				cur.execute(sql)
				vals = [row[0] for row in cur.fetchall()]
		except Exception as exc:
			print(f"Total unit drill filter options failed for {field['key']}:", exc)
			vals = []
		options[field['key']] = [str(v).strip() for v in vals if v]
	return options


def _fetch_total_unit_summary(filter_clauses, filter_params, metric_columns):
	summary = {
		'unit_count': 0,
		'occupancy_pct': None,
		'exposure_8_weeks': None,
		'vacant_units': None,
		'ntv_units': None,
		'exposure_units': None,
	}

	sql_parts = ["COUNT(*) AS total_units"]
	resident_col = metric_columns.get('resident_name')
	if resident_col:
		ident = _quote_ident(resident_col)
		sql_parts.append(
			f"COUNT(*) FILTER (WHERE COALESCE(NULLIF({ident}::text, ''), NULL) IS NOT NULL) AS occupied_units"
		)
		sql_parts.append(
			f"COUNT(*) FILTER (WHERE COALESCE(NULLIF({ident}::text, ''), NULL) IS NULL) AS vacant_units"
		)
	ntv_col = metric_columns.get('ntv_flag')
	if ntv_col:
		ntv_ident = _quote_ident(ntv_col)
		sql_parts.append(
			f"COUNT(*) FILTER (WHERE COALESCE(NULLIF({ntv_ident}::text, ''), NULL) IS NOT NULL) AS ntv_units"
		)
	exposure_col = metric_columns.get('exposure_8_weeks')
	if exposure_col:
		sql_parts.append(f"AVG({_clean_numeric_expr(exposure_col)}) AS exposure_8_weeks")

	sql = f"SELECT {', '.join(sql_parts)} FROM {TOTAL_UNIT_DRILL_VIEW}"
	if filter_clauses:
		sql += ' WHERE ' + ' AND '.join(filter_clauses)

	try:
		with connection.cursor() as cur:
			cur.execute(sql, filter_params)
			row = cur.fetchone()
			col_names = [desc[0] for desc in cur.description]
			data = dict(zip(col_names, row if row else []))
	except Exception as exc:
		print('Total unit drill summary query failed:', exc)
		return summary

	total_units = data.get('total_units') or 0
	summary['unit_count'] = total_units
	occupied_units = data.get('occupied_units')
	vacant_units = data.get('vacant_units')
	if vacant_units is not None:
		summary['vacant_units'] = int(vacant_units)
	if data.get('ntv_units') is not None:
		summary['ntv_units'] = int(data['ntv_units'])
	if total_units and occupied_units is not None:
		summary['occupancy_pct'] = round((occupied_units / total_units) * 100, 2)
	elif total_units and vacant_units is not None:
		occupied = total_units - vacant_units
		summary['occupancy_pct'] = round((occupied / total_units) * 100, 2)
	if data.get('exposure_8_weeks') is not None:
		try:
			summary['exposure_8_weeks'] = float(data['exposure_8_weeks'])
		except Exception:
			summary['exposure_8_weeks'] = data['exposure_8_weeks']
	exposure_units, exposure_ratio = _compute_exposure_8_weeks(filter_clauses, filter_params, metric_columns, total_units)
	if exposure_units is not None:
		summary['exposure_units'] = exposure_units
		if exposure_ratio is not None:
			summary['exposure_8_weeks'] = round(exposure_ratio * 100, 2)
	vacancy_metrics = _compute_vacancy_ntv_metrics(filter_clauses, filter_params, metric_columns, total_units)
	if vacancy_metrics:
		vacant_count = vacancy_metrics.get('vacant_total')
		if vacant_count is None:
			vacant_count = (vacancy_metrics.get('vacant_not_leased') or 0) + (vacancy_metrics.get('vacant_leased') or 0)
		ntv_count = vacancy_metrics.get('ntv_total') or 0
		summary['vacant_units'] = vacant_count
		summary['ntv_units'] = ntv_count
		if total_units:
			summary['occupancy_pct'] = round(((total_units - vacant_count) / total_units) * 100, 2)
	return summary


def _compute_exposure_8_weeks(filter_clauses, filter_params, metric_columns, total_units):
	if not total_units:
		return None, None
	unit_type_col = metric_columns.get('unit_type')
	move_out_col = metric_columns.get('move_out_date')
	if not unit_type_col or not move_out_col:
		return None, None
	where_sql = ' WHERE ' + ' AND '.join(filter_clauses) if filter_clauses else ''
	cutoff = (date.today() + timedelta(days=56)).strftime('%Y-%m-%d')
	sql = f"""
		SELECT
			COUNT(*) FILTER (
				WHERE {_quote_ident(unit_type_col)} IN (%s, %s)
				  AND {_quote_ident(move_out_col)}::date <= %s
			) AS vacant_not_leased,
			COUNT(*) FILTER (
				WHERE {_quote_ident(unit_type_col)} = %s
				  AND {_quote_ident(move_out_col)}::date <= %s
			) AS ntv_not_leased
		FROM {TOTAL_UNIT_DRILL_VIEW}
	{where_sql}
	"""
	params = [
		VACANT_NOT_LEASED_STATUSES[0],
		VACANT_NOT_LEASED_STATUSES[1],
		cutoff,
		NTV_NOT_LEASED_STATUS,
		cutoff,
	] + list(filter_params)
	try:
		with connection.cursor() as cur:
			cur.execute(sql, params)
			row = cur.fetchone()
	except Exception as exc:
		print('Total unit drill exposure calculation failed:', exc)
		row = None
	if not row:
		return None, None
	vacant_count = row[0] or 0
	ntv_count = row[1] or 0
	exposure_units = vacant_count + ntv_count
	if exposure_units and total_units:
		ratio = exposure_units / total_units
	else:
		ratio = 0 if total_units else None
	return exposure_units, ratio



def _compute_vacancy_ntv_metrics(filter_clauses, filter_params, metric_columns, total_units):
	unit_type_col = metric_columns.get('unit_type')
	if not unit_type_col:
		return {}
	type_ident = _quote_ident(unit_type_col)
	scheduled_move_in_col = metric_columns.get('scheduled_move_in')
	schedule_ident = _quote_ident(scheduled_move_in_col) if scheduled_move_in_col else None
	vacant_not_placeholders = ', '.join(['%s'] * len(VACANT_NOT_LEASED_TYPES))
	vacant_leased_placeholders = ', '.join(['%s'] * len(VACANT_LEASED_TYPES))
	ntv_placeholders = ', '.join(['%s'] * len(NTV_TYPES))
	where_sql = ' WHERE ' + ' AND '.join(filter_clauses) if filter_clauses else ''
	leased_condition = f"{type_ident} IN ({vacant_leased_placeholders})"
	params = list(VACANT_NOT_LEASED_TYPES) + list(VACANT_LEASED_TYPES)
	if schedule_ident:
		leased_condition += f" AND {schedule_ident}::date >= %s"
		params.append(date.today().strftime('%Y-%m-%d'))
	params += list(NTV_TYPES) + list(filter_params)
	sql = f"""
		SELECT
			COUNT(*) FILTER (WHERE {type_ident} IN ({vacant_not_placeholders})) AS vacant_not_leased,
			COUNT(*) FILTER (WHERE {leased_condition}) AS vacant_leased,
			COUNT(*) FILTER (WHERE {type_ident} IN ({ntv_placeholders})) AS ntv_total
		FROM {TOTAL_UNIT_DRILL_VIEW}
	{where_sql}
	"""
	try:
		with connection.cursor() as cur:
			cur.execute(sql, params)
			row = cur.fetchone()
	except Exception as exc:
		print('Total unit drill vacancy metrics failed:', exc)
		return {}
	if not row:
		return {}
	return {
		'vacant_not_leased': row[0] or 0,
		'vacant_leased': row[1] or 0,
		'vacant_total': (row[0] or 0) + (row[1] or 0),
		'ntv_total': row[2] or 0,
	}

def _prepare_total_unit_drill_query(request):
	columns = _get_total_unit_drill_columns()
	if not columns:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {'unit_count': 0},
				'filter_options': {},
				'filters': {},
				'error': 'The total unit drill-through view is currently unavailable.',
			},
		}

	select_parts = []
	display_columns = []
	alias_order = []
	alias_to_source = {}
	for field in TOTAL_UNIT_TABLE_FIELDS:
		col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			continue
		alias = field['key']
		alias_to_source[alias] = col
		alias_order.append(alias)
		display_columns.append({'key': alias, 'label': field['label']})
		select_parts.append(f"{_quote_ident(col)} AS {alias}")

	filter_options = _fetch_total_unit_filter_options(columns)

	if not select_parts:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {'unit_count': 0},
				'filter_options': filter_options,
				'filters': {},
				'error': 'No recognizable columns were found for the total unit drill-through dataset.',
			},
		}

	filter_clauses = []
	filter_params = []
	active_filters = {}
	for field in TOTAL_UNIT_FILTER_FIELDS:
		values = _extract_filter_list(request, request.GET, field['key'])
		clean = [v.strip() for v in values if v and v.strip() and v.strip().lower() != 'all'] if values else []
		active_filters[field['key']] = values[0] if values else ''
		if not clean:
			continue
		col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			continue
		clause_parts = []
		for val in clean:
			clause_parts.append(f"{_quote_ident(col)} ILIKE %s")
			filter_params.append(f"%{val}%")
		if clause_parts:
			filter_clauses.append('(' + ' OR '.join(clause_parts) + ')')

	return {
		'error': False,
		'columns': columns,
		'display_columns': display_columns,
		'alias_order': alias_order,
		'alias_to_source': alias_to_source,
		'select_parts': select_parts,
		'filter_clauses': filter_clauses,
		'filter_params': filter_params,
		'active_filters': active_filters,
		'filter_options': filter_options,
	}


def _build_total_unit_drill_context(request):
	query_info = _prepare_total_unit_drill_query(request)
	if query_info.get('error'):
		return query_info['error_context']

	columns = query_info['columns']
	display_columns = query_info['display_columns']
	alias_order = query_info['alias_order']
	alias_to_source = query_info['alias_to_source']
	select_parts = query_info['select_parts']
	filter_clauses = query_info['filter_clauses']
	filter_params = query_info['filter_params']
	active_filters = query_info['active_filters']
	filter_options = query_info['filter_options']

	metric_columns = {}
	for key, keywords in TOTAL_UNIT_METRIC_HINTS.items():
		if key in alias_to_source:
			metric_columns[key] = alias_to_source[key]
		else:
			metric_columns[key] = _find_column_by_keywords(columns, keywords)

	summary = _fetch_total_unit_summary(filter_clauses, filter_params, metric_columns)
	total_units = summary.get('unit_count') or 0
	page_size = TOTAL_UNIT_DRILL_PAGE_SIZE
	page_param = request.GET.get('page') if hasattr(request, 'GET') else None
	try:
		requested_page = int(page_param) if page_param else 1
	except Exception:
		requested_page = 1
	if requested_page < 1:
		requested_page = 1
	if total_units:
		total_pages = (total_units + page_size - 1) // page_size
		page = min(requested_page, total_pages)
	else:
		total_pages = 1
		page = 1
	offset = (page - 1) * page_size if total_units else 0

	where_sql = ' WHERE ' + ' AND '.join(filter_clauses) if filter_clauses else ''
	order_alias = 'property_name' if 'property_name' in alias_order else (alias_order[0] if alias_order else None)
	data_sql = f"SELECT {', '.join(select_parts)} FROM {TOTAL_UNIT_DRILL_VIEW}{where_sql}"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} NULLS LAST"
	data_sql += " LIMIT %s OFFSET %s"
	data_params = list(filter_params) + [page_size, offset]

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, data_params)
			result = cur.fetchall()
	except Exception as exc:
		print('Total unit drill-through data query failed:', exc)
		result = []
	for raw in result:
		row_dict = dict(zip(alias_order, raw))
		ordered_values = [_format_total_unit_value(alias, row_dict.get(alias)) for alias in alias_order]
		rows.append(ordered_values)

	start_index = offset + 1 if total_units and rows else 0
	end_index = offset + len(rows)
	if total_units and end_index > total_units:
		end_index = total_units

	base_query_pairs = []
	if hasattr(request.GET, 'lists'):
		for key, values in request.GET.lists():
			if key == 'page':
				continue
			for val in values:
				if val:
					base_query_pairs.append((key, val))
	base_drill_url = reverse('total_units_drillthrough')

	def _build_page_url(target_page):
		pairs = list(base_query_pairs)
		if target_page > 1:
			pairs.append(('page', target_page))
		query = urlencode(pairs, doseq=True)
		return f"{base_drill_url}?{query}" if query else base_drill_url

	pagination = {
		'page': page,
		'page_size': page_size,
		'total_pages': total_pages,
		'has_prev': page > 1,
		'has_next': bool(total_units and page < total_pages),
		'prev_url': _build_page_url(page - 1) if page > 1 else '',
		'next_url': _build_page_url(page + 1) if total_units and page < total_pages else '',
		'start_index': start_index,
		'end_index': end_index,
		'total_results': total_units,
	}

	export_pairs = [(k, v) for (k, v) in base_query_pairs if k != 'return_url']
	export_base = reverse('total_units_drillthrough_export')
	export_query = urlencode(export_pairs, doseq=True)
	export_url = f"{export_base}?{export_query}" if export_query else export_base

	return {
		'table_columns': display_columns,
		'table_rows': rows,
		'metrics': summary,
		'filter_options': filter_options,
		'filters': active_filters,
		'limit_reached': bool(pagination['has_next'] or (pagination['start_index'] > 1)),
		'row_count': len(rows),
		'pagination': pagination,
		'export_url': export_url,
	}



def _fetch_finance_scorecard_metrics_orm(request, params, svc_ctx):
	"""Try fetching finance KPIs via the Django ORM."""
	# Django's ORM struggles with identifiers that contain percent signs (e.g., "noi_as_%_of_revenue").
	# When such columns exist, skip the ORM path altogether to avoid noisy errors and rely on SQL fallback.
	columns = _get_finance_scorecard_columns()
	if any('%' in (col or '') for col in columns):
		return {}
	try:
		qs = FinanceKpiScorecard.objects.all()
	except Exception as exc:
		print('Finance KPI ORM base query failed:', exc)
		return {}

	inv_values = _extract_filter_list(request, params, 'investor')
	if inv_values:
		qs = qs.filter(investor__in=inv_values)

	manager_values = _extract_filter_list(request, params, 'regional_manager')
	if manager_values:
		qs = qs.filter(regional_area_manager__in=manager_values)

	community_values = _extract_filter_list(request, params, 'community')
	if community_values:
		qs = qs.filter(community__in=community_values)

	month_starts = _resolve_selected_month_starts(request, params, svc_ctx)
	if month_starts:
		month_ends = []
		for dt in month_starts:
			if not dt:
				continue
			last_day = calendar.monthrange(dt.year, dt.month)[1]
			month_ends.append(date(dt.year, dt.month, last_day))
		qs = qs.filter(month_end_date__in=month_ends)
	else:
		latest_period = qs.order_by('-month_end_date').values_list('month_end_date', flat=True).first()
		if latest_period:
			qs = qs.filter(month_end_date=latest_period)

	try:
		field_map = [
			('yoy_change_operating_revenue', 'yoy_operating_revenue'),
			('yoy_change_expense', 'yoy_operating_expense'),
			('noi_percent_revenue', 'noi_percent_revenue'),
			('executed_rent_yoy', 'executed_rent_yoy'),
			('in_place_rent_per_sqft', 'in_place_rent_per_sqft'),
		]
		rows = list(qs.values_list(*[field for _, field in field_map]))
		if not rows:
			return {}
		metrics = {}
		for idx, (alias, _) in enumerate(field_map):
			values = [row[idx] for row in rows if row[idx] is not None]
			metrics[alias] = (sum(values) / len(values)) if values else None
		return metrics
	except Exception as exc:
		print('Finance KPI ORM aggregation failed:', exc)
		return {}


def _fetch_finance_scorecard_metrics_sql(request, params, svc_ctx):
	"""Fetch aggregated finance KPI metrics from the scorecard view via raw SQL."""
	columns = _get_finance_scorecard_columns()
	if not columns:
		return {}

	metric_specs = [
		('yoy_change_operating_revenue', ['operating', 'revenue', 'yoy']),
		('yoy_change_expense', ['expense', 'yoy']),
		('noi_percent_revenue', ['noi', 'revenue']),
		('executed_rent_yoy', ['executed', 'rent', 'yoy']),
		('in_place_rent_per_sqft', ['place', 'rent', 'sq'])
	]
	selected_metrics = []
	for alias, keywords in metric_specs:
		col = _find_column_by_keywords(columns, keywords)
		if col:
			selected_metrics.append((alias, col))

	if not selected_metrics:
		return {}

	select_clause = ', '.join([f"AVG({_quote_ident(col)}) AS {alias}" for alias, col in selected_metrics])
	sql = f"SELECT {select_clause} FROM web_ai.finance_kpi_scorecard"
	filter_clauses = []
	filter_params = []

	investor_col = _find_exact_column(columns, 'investor') or _find_column_by_keywords(columns, ['investor'])
	manager_col = _find_exact_column(columns, 'regional_area_manager') or _find_column_by_keywords(columns, ['regional', 'manager'])
	community_col = (
		_find_exact_column(columns, 'community')
		or _find_exact_column(columns, 'property_name')
		or _find_column_by_keywords(columns, ['community'])
		or _find_column_by_keywords(columns, ['property', 'name'])
	)
	date_col = None
	for patterns in (['month', 'end', 'date'], ['enddateofmonth'], ['snapshot', 'date'], ['period', 'date']):
		candidate = _find_column_by_keywords(columns, patterns)
		if candidate:
			date_col = candidate
			break

	def _build_in_clause(col_name, values):
		if not values:
			return None, []
		placeholders = ','.join(['%s'] * len(values))
		return f"{_quote_ident(col_name)} IN ({placeholders})", list(values)

	inv_values = _extract_filter_list(request, params, 'investor')
	if investor_col and inv_values:
		investor_clauses = []
		investor_params = []
		for val in inv_values:
			text = (val or '').strip()
			if not text or text.lower() == 'all':
				continue
			investor_clauses.append(f"{_quote_ident(investor_col)} ILIKE %s")
			investor_params.append(f"%{text}%")
		if investor_clauses:
			filter_clauses.append('(' + ' OR '.join(investor_clauses) + ')')
			filter_params.extend(investor_params)
		else:
			inv_values = []

	manager_values = _extract_filter_list(request, params, 'regional_manager')
	if manager_col and manager_values:
		clause, vals = _build_in_clause(manager_col, manager_values)
		if clause:
			filter_clauses.append(clause)
			filter_params.extend(vals)

	community_values = _extract_filter_list(request, params, 'community')
	if community_col and community_values:
		community_clauses = []
		community_params = []
		for val in community_values:
			text = (val or '').strip()
			if not text:
				continue
			community_clauses.append(f"{_quote_ident(community_col)} ILIKE %s")
			community_params.append(f"%{text}%")
		if community_clauses:
			filter_clauses.append('(' + ' OR '.join(community_clauses) + ')')
			filter_params.extend(community_params)

	where_clauses = list(filter_clauses)
	sql_params = list(filter_params)

	def _latest_month_for_filters():
		if not date_col:
			return None
			
		base_sql = f"SELECT DATE_TRUNC('month', MAX({_quote_ident(date_col)}))::date FROM web_ai.finance_kpi_scorecard"
		if filter_clauses:
			base_sql += ' WHERE ' + ' AND '.join(filter_clauses)
		try:
			with connection.cursor() as cur:
				cur.execute(base_sql, filter_params)
				row = cur.fetchone()
		except Exception as exc:
			print('Finance KPI scorecard latest month lookup failed:', exc)
			return None
		return row[0] if row and row[0] else None

	month_starts = _resolve_selected_month_starts(request, params, svc_ctx)
	date_params = []
	if date_col and month_starts:
		placeholders = ','.join(['%s'] * len(month_starts))
		where_clauses.append(f"DATE_TRUNC('month', {_quote_ident(date_col)})::date IN ({placeholders})")
		date_params.extend([dt.strftime('%Y-%m-01') for dt in month_starts])
	elif date_col:
		latest_month = _latest_month_for_filters()
		if latest_month:
			where_clauses.append(f"DATE_TRUNC('month', {_quote_ident(date_col)})::date = %s")
			date_params.append(latest_month.strftime('%Y-%m-%d'))

	if date_params:
		sql_params.extend(date_params)

	if where_clauses:
		sql += ' WHERE ' + ' AND '.join(where_clauses)

	try:
		with connection.cursor() as cur:
			cur.execute(sql, sql_params)
			row = cur.fetchone()
	except Exception as exc:
		print('Finance KPI scorecard query failed:', exc)
		return {}

	if not row:
		return {}

	metrics = {}
	for idx, (alias, _) in enumerate(selected_metrics):
		value = row[idx] if idx < len(row) else None
		if value is not None:
			try:
				metrics[alias] = float(value)
			except Exception:
				metrics[alias] = value
		else:
			metrics[alias] = None
	return metrics


def fetch_finance_scorecard_metrics(request, params, svc_ctx):
	metrics = _fetch_finance_scorecard_metrics_orm(request, params, svc_ctx)
	if metrics and any(value is not None for value in metrics.values()):
		return metrics
	return _fetch_finance_scorecard_metrics_sql(request, params, svc_ctx)


def format_finance_kpi_values(raw_values):
	"""Return display-ready strings for finance KPIs."""
	def fmt_percent(val):
		if val is None:
			return '--'
		try:
			val = float(val)
		except Exception:
			return '--'
		if abs(val) <= 1:
			val *= 100
		return f"{val:.2f}%"

	def fmt_currency(val):
		if val is None:
			return '--'
		try:
			val = float(val)
		except Exception:
			return '--'
		return f"${val:,.2f}"

	return {
		'in_place_rent_per_sqft': fmt_currency(raw_values.get('in_place_rent_per_sqft')),
		'yoy_change_operating_revenue': fmt_percent(raw_values.get('yoy_change_operating_revenue')),
		'yoy_change_expense': fmt_percent(raw_values.get('yoy_change_expense')),
		'noi_percent_revenue': fmt_percent(raw_values.get('noi_percent_revenue')),
		'executed_rent_yoy': fmt_percent(raw_values.get('executed_rent_yoy')),
	}


@login_required
def total_units_drillthrough(request):
	context = _build_total_unit_drill_context(request)
	context['filter_fields'] = [{'key': field['key'], 'label': field['label']} for field in TOTAL_UNIT_FILTER_FIELDS]
	context['page_title'] = 'Total Unit Drill-Through'
	context['row_limit'] = TOTAL_UNIT_DRILL_PAGE_SIZE
	context.setdefault('error', '')
	context.setdefault('export_url', '')
	dashboard_return_url = request.GET.get('return_url') or reverse('dashboard')
	context['dashboard_return_url'] = dashboard_return_url
	reset_base = reverse('total_units_drillthrough')
	if request.GET.get('return_url'):
		context['drill_reset_url'] = f"{reset_base}?{urlencode({'return_url': request.GET.get('return_url')})}"
	else:
		context['drill_reset_url'] = reset_base
	filters = context.get('filters') or {}
	filter_options = context.get('filter_options') or {}
	filter_blocks = []
	for field in context['filter_fields']:
		key = field['key']
		filter_blocks.append({
			'key': key,
			'label': field['label'],
			'options': filter_options.get(key, []),
			'selected': filters.get(key, ''),
		})
	context['filter_blocks'] = filter_blocks
	return render(request, 'dashboard/total_units_drillthrough.html', context)


@login_required
def total_units_drillthrough_export(request):
	query_info = _prepare_total_unit_drill_query(request)
	if query_info.get('error'):
		message = query_info['error_context'].get('error') if query_info.get('error_context') else 'The dataset is unavailable.'
		return HttpResponse(message or 'The dataset is unavailable.', status=400)

	alias_order = query_info['alias_order']
	select_parts = query_info['select_parts']
	if not alias_order or not select_parts:
		return HttpResponse('No columns are available for export.', status=400)

	filter_clauses = query_info['filter_clauses']
	filter_params = query_info['filter_params']
	display_columns = query_info['display_columns']

	where_sql = ' WHERE ' + ' AND '.join(filter_clauses) if filter_clauses else ''
	order_alias = 'property_name' if 'property_name' in alias_order else (alias_order[0] if alias_order else None)
	data_sql = f"SELECT {', '.join(select_parts)} FROM {TOTAL_UNIT_DRILL_VIEW}{where_sql}"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} NULLS LAST"

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, list(filter_params))
			rows = cur.fetchall()
	except Exception as exc:
		print('Total unit drill-through export failed:', exc)
		return HttpResponse('Failed to export data.', status=500)

	timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
	filename = f"total_units_drillthrough_{timestamp}.csv"
	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = f'attachment; filename="{filename}"'

	writer = csv.writer(response)
	writer.writerow([col['label'] for col in display_columns])
	for raw in rows:
		row_dict = dict(zip(alias_order, raw))
		writer.writerow([_format_total_unit_value(alias, row_dict.get(alias)) for alias in alias_order])

	return response

@login_required
def dashboard(request):
	user = request.user
	# If the user didn't supply any period-related parameters, default to the
	# PREVIOUS month (server-side) to ensure complete data is shown.
	# Current month data may be incomplete since it's still being collected.
	period_params = ['period_mode', 'period_year', 'period_quarter', 'period_month', 'period']
	has_period_param = any([p in request.GET and request.GET.get(p) for p in period_params])
	if not has_period_param:
		# Inspect available periods via the DashboardPageService so the server picks
		# the PREVIOUS month relative to current date.
		svc_probe = DashboardPageService({})
		svc_periods = svc_probe.get_context().get('periods') or {}
		# periods structure: { '2025': { 'months': [...], 'quarters': {...} }, ... }
		if svc_periods:
			# Calculate previous month (not latest month)
			try:
				today = datetime.now()
				current_month = today.month
				current_year = today.year
				
				# Calculate previous month
				if current_month == 1:
					prev_month = 12
					prev_year = current_year - 1
				else:
					prev_month = current_month - 1
					prev_year = current_year
				
				prev_month_name = datetime.strptime(f"{prev_month:02d}", "%m").strftime("%b")
				prev_year_str = str(prev_year)
				
				# Check if previous month exists in our data
				if prev_year_str in svc_periods:
					year_data = svc_periods[prev_year_str]
					if prev_month_name in year_data.get('months', []):
						# Previous month found in data
						params = {'period_mode': 'month', 'period_month': f"{prev_month_name}-{prev_year_str}"}
					else:
						# Previous month not in data, fall back to latest available
						latest_year = next(iter(sorted(svc_periods.keys(), reverse=True)))
						quarters = svc_periods.get(latest_year, {}).get('quarters', {})
						if quarters:
							latest_quarter = next(iter(sorted(quarters.keys(), reverse=True)))
							months = quarters.get(latest_quarter, [])
							if months:
								latest_month = sorted(months, key=lambda m: datetime.strptime(m, '%b'))[-1]
								params = {'period_mode': 'month', 'period_month': f"{latest_month}-{latest_year}"}
							else:
								params = {'period_mode': 'year', 'period_year': 'all'}
						else:
							# no quarters -> try months top-level (compat)
							months_top = svc_periods.get(latest_year, {}).get('months') or []
							if months_top:
								latest_month = sorted(months_top, key=lambda m: datetime.strptime(m, '%b'))[-1]
								params = {'period_mode': 'month', 'period_month': f"{latest_month}-{latest_year}"}
							else:
								params = {'period_mode': 'year', 'period_year': 'all'}
				else:
					# Previous year not in data, fall back to latest available
					latest_year = next(iter(sorted(svc_periods.keys(), reverse=True)))
					quarters = svc_periods.get(latest_year, {}).get('quarters', {})
					if quarters:
						latest_quarter = next(iter(sorted(quarters.keys(), reverse=True)))
						months = quarters.get(latest_quarter, [])
						if months:
							latest_month = sorted(months, key=lambda m: datetime.strptime(m, '%b'))[-1]
							params = {'period_mode': 'month', 'period_month': f"{latest_month}-{latest_year}"}
						else:
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

	finance_kpi_raw = fetch_finance_scorecard_metrics(request, params, svc_ctx)
	finance_kpi_display = format_finance_kpi_values(finance_kpi_raw) if finance_kpi_raw else format_finance_kpi_values({})

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
			# Check if Livcor/BLACKSTONE selected
			inv_list_up = [x.upper() for x in inv_list] if inv_list else []
			livcor_selected = any(x in inv_list_up for x in ['BLACKSTONE/LIVCOR', 'LIVCOR'])
			if _period_after_jun_2025_local(sel_mode, year, sel_period, svc_ctx.get('period_quarter')) and not livcor_selected:
				monthly_qs = monthly_qs.exclude(investor__iexact='BLACKSTONE/LIVCOR').exclude(investor__iexact='Livcor')
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
			# When multiple years selected or viewing from modal, show all
			rows = list(grouped)
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
			# When multiple periods selected or viewing from modal, show all
			rows = list(grouped)

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
		# For pooled averages (avg_turn_time, renewal_conv, exposure, delinquency) compute numerator/denominator
		turn_num = 0.0
		turn_den = 0
		rc_num = 0.0
		rc_den = 0
		exp_num = 0.0
		exp_den = 0
		del_num = 0.0
		del_den = 0
		
		# Debug logging
		print(f"\n{'='*80}")
		print(f"DASHBOARD KPI AGGREGATION")
		print(f"{'='*80}")
		print(f"Total rows after aggregation: {len(rows)}")
		print(f"Community filter: {community_list}")
		print(f"Selected months: {selected_months}")
		print(f"Period mode: {sel_mode}")
		print(f"{'='*80}\n")
		
		from datetime import datetime as _dt
		for r in rows:
			# Debug: log raw row data
			print(f"Row data: year={r.get('year')}, month={r.get('month')}, cnt={r.get('cnt')}, delinquency_sum={r.get('delinquency_sum')}, exposure_sum={r.get('exposure_sum')}")
			
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
			
			cnt = int(r.get('cnt') or 0)
			
			# Average exposure (convert to percentage by dividing by 100)
			exp = r.get('exposure_sum')
			if exp is not None and cnt:
				exp_num += float(exp)
				exp_den += cnt
			
			# Average delinquency (convert to percentage by dividing by 100)
			# Delinquency is stored as raw number in DB, need to convert to percentage
			del_val = r.get('delinquency_sum')
			if del_val is not None and cnt:
				del_num += float(del_val)
				del_den += cnt
				# Debug logging
				print(f"  -> Delinquency accumulation: del_sum={del_val}, cnt={cnt}, cumulative del_num={del_num}, del_den={del_den}")
			
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
		if exp_den:
			# Exposure is stored as decimal in DB (0.132), convert to percentage (13.2%)
			kpi_overrides['exposure'] = round((exp_num / exp_den) * 100, 3)
		if del_den:
			# Delinquency is stored as decimal in DB (0.122), convert to percentage (12.2%)
			kpi_overrides['delinquency'] = round((del_num / del_den) * 100, 3)
			# Debug logging
			print(f"\n{'*'*60}")
			print(f"FINAL DELINQUENCY KPI CALCULATION")
			print(f"{'*'*60}")
			print(f"del_num (sum): {del_num}")
			print(f"del_den (count): {del_den}")
			print(f"Result (del_num/del_den) * 100: {kpi_overrides['delinquency']}")
			print(f"{'*'*60}\n")
		if turn_den:
			kpi_overrides['avg_turn_time'] = round(turn_num / turn_den, 1)
		if rc_den:
			# Renewal conversion is stored as decimal in DB (0.85), convert to percentage (85%)
			kpi_overrides['renewal_conversion'] = round((rc_num / rc_den) * 100, 1)

		# Build delinquency trend chart
		# For more accurate data, collect delinquency per period
		delinquency_data = []
		for r in rows:
			del_sum = r.get('delinquency_sum')
			cnt = int(r.get('cnt') or 1)
			if del_sum is not None and cnt > 0:
				# Average delinquency for this period
				# DB stores as decimal (0.122), convert to percentage (12.2%)
				avg_delinquency = (float(del_sum) / cnt) * 100
				delinquency_data.append(round(avg_delinquency, 3))
			else:
				delinquency_data.append(0.0)
		
		chart_delinquency = {
			'labels': labels,
			'data': delinquency_data
		}

		# Build exposure trend chart
		exposure_data = []
		for r in rows:
			exp_sum = r.get('exposure_sum')
			cnt = int(r.get('cnt') or 1)
			if exp_sum is not None and cnt > 0:
				# Average exposure for this period
				# DB stores as decimal (0.132), convert to percentage (13.2%)
				avg_exposure = (float(exp_sum) / cnt) * 100
				exposure_data.append(round(avg_exposure, 3))
			else:
				exposure_data.append(0.0)
		
		chart_exposure = {
			'labels': labels,
			'data': exposure_data
		}

		# Build avg turn time trend chart
		avg_turn_time_data = []
		for r in rows:
			turn_sum = r.get('avg_turn')
			cnt = int(r.get('cnt') or 1)
			if turn_sum is not None and cnt > 0:
				# Average turn time for this period
				avg_turn = float(turn_sum) * cnt / cnt  # weighted average
				avg_turn_time_data.append(round(avg_turn, 1))
			else:
				avg_turn_time_data.append(0.0)
		
		chart_avg_turn_time = {
			'labels': labels,
			'data': avg_turn_time_data
		}

		# Build service requests trend chart
		service_requests_data = []
		for r in rows:
			sr_sum = r.get('service_requests_sum')  # Fixed: was 'service_request_sum'
			if sr_sum is not None:
				service_requests_data.append(int(sr_sum))
			else:
				service_requests_data.append(0)
		
		chart_service_requests = {
			'labels': labels,
			'data': service_requests_data
		}

		# Build renewal conversion trend chart
		renewal_conversion_data = []
		for r in rows:
			rc_sum = r.get('renewal_conv_avg')
			cnt = int(r.get('cnt') or 1)
			if rc_sum is not None and cnt > 0:
				# Average renewal conversion for this period
				# DB stores as decimal (0.85), convert to percentage (85%)
				avg_rc = (float(rc_sum) * cnt / cnt) * 100  # weighted average
				renewal_conversion_data.append(round(avg_rc, 1))
			else:
				renewal_conversion_data.append(0.0)
		
		chart_renewal_conversion = {
			'labels': labels,
			'data': renewal_conversion_data
		}
		# NOTE: KPI overrides and chart payloads will be applied to the template context
		# after the primary context dict is built further below. We store them in
		# local variables here (chart_renewals, chart_expense, chart_delinquency, chart_exposure,
		# chart_avg_turn_time, chart_service_requests, chart_renewal_conversion, kpi_overrides).

	except Exception as e:
		# On any failure, provide empty chart payloads and no KPI overrides
		print(f"\n{'!'*80}")
		print(f"EXCEPTION IN DASHBOARD KPI AGGREGATION")
		print(f"{'!'*80}")
		print(f"Exception: {str(e)}")
		import traceback
		traceback.print_exc()
		print(f"{'!'*80}\n")
		
		chart_renewals = {'labels': [], 'datasets': []}
		chart_expense = {'labels': [], 'datasets': []}
		chart_delinquency = {'labels': [], 'data': []}
		chart_exposure = {'labels': [], 'data': []}
		chart_avg_turn_time = {'labels': [], 'data': []}
		chart_service_requests = {'labels': [], 'data': []}
		chart_renewal_conversion = {'labels': [], 'data': []}
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
		'finance_kpi': finance_kpi_display,
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
		'delinquency_chart_data': chart_delinquency,
		'exposure_chart_data': chart_exposure,
		'avg_turn_time_chart_data': chart_avg_turn_time,
		'service_requests_chart_data': chart_service_requests,
		'renewal_conversion_chart_data': chart_renewal_conversion,
	}

	# Build drill-through URL that preserves compatible filters from the main dashboard.
	drill_filter_keys = {field['key'] for field in TOTAL_UNIT_FILTER_FIELDS}
	drill_pairs = []
	if hasattr(request.GET, 'lists'):
		for key, values in request.GET.lists():
			if key in drill_filter_keys:
				for val in values:
					if val:
						drill_pairs.append((key, val))
	return_target = request.get_full_path()
	if return_target:
		drill_pairs.append(('return_url', return_target))
	base_drill_url = reverse('total_units_drillthrough')
	if drill_pairs:
		context['total_unit_drill_url'] = f"{base_drill_url}?{urlencode(drill_pairs, doseq=True)}"
	else:
		context['total_unit_drill_url'] = base_drill_url

	# Apply KPI overrides computed by the consolidated monthly aggregation (if any)
	try:
		print(f"\n{'='*80}")
		print(f"APPLYING KPI OVERRIDES")
		print(f"{'='*80}")
		print(f"KPI Overrides: {kpi_overrides}")
		
		kpi = context.get('kpi') or {}
		print(f"Original KPI delinquency: {kpi.get('delinquency', 'NOT SET')}")
		
		for k, v in (kpi_overrides or {}).items():
			kpi[k] = v
		context['kpi'] = kpi
		
		print(f"Final KPI delinquency: {kpi.get('delinquency', 'NOT SET')}")
		print(f"{'='*80}\n")
	except Exception as ex:
		# keep existing KPIs on unexpected failures
		print(f"EXCEPTION applying KPI overrides: {str(ex)}")
		import traceback
		traceback.print_exc()
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
			livcor_selected = any(x in sel_up for x in ['BLACKSTONE/LIVCOR', 'LIVCOR'])
			if livcor_selected:
				# user explicitly selected Livcor/BLACKSTONE — preserve it and any missing selections
				missing_invs = [inv for inv in selected_inv_list if inv and inv not in _unfiltered_investors]
				if missing_invs:
					context['investors'] = _unfiltered_investors + missing_invs
				else:
					context['investors'] = _unfiltered_investors
			else:
				# period is after Jun-2025 and user did NOT explicitly choose Livcor — hide it
				context['investors'] = [i for i in _unfiltered_investors if i.upper() not in ['BLACKSTONE/LIVCOR', 'LIVCOR']]
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
				'delinquency_data': context.get('delinquency_chart_data'),
				'exposure_data': context.get('exposure_chart_data'),
				'avg_turn_time_data': context.get('avg_turn_time_chart_data'),
				'service_requests_data': context.get('service_requests_chart_data'),
				'renewal_conversion_data': context.get('renewal_conversion_chart_data'),
				'selected_period': svc_ctx.get('selected_period')
			})
		# Default AJAX response for KPI/table updates
		kpi_html = render_to_string('dashboard/partials/_kpi_cards.html', context=context, request=request)
		table_html = render_to_string('dashboard/partials/_property_table.html', context=context, request=request)
		return JsonResponse({'kpi_html': kpi_html, 'table_html': table_html, 'selected_period': svc_ctx.get('selected_period')})

	return render(request, 'dashboard/dashboard.html', context)
