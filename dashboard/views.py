from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Max, Sum, Avg, Count, Q, F, DateField
from django.urls import reverse
from .models import (OlympusLeaseTrendAnalysis, OlympusLeaseKpisTrendMonthly, 
					 FinanceKpiScorecard, DelinquencyDrillThrough, UnitLevelDrillThrough, CardTransactions)
from datetime import datetime, date, timedelta
from django.db.models.functions import ExtractYear, ExtractMonth, TruncMonth, Coalesce
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
from django.utils.safestring import mark_safe
from collections import OrderedDict


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
			except Exception:
				# Skip invalid year strings
				continue
			for month in range(1, 13):
				label = datetime(target_year, month, 1).strftime('%b-%Y')
				target_dates.append({'year': target_year, 'month': month, 'label': label})
				period_labels.append(label)

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

	# Handle explicit month selections (e.g., 'Jan-2026') — add to target dates
	if selected_months:
		for m in selected_months:
			try:
				dt = datetime.strptime(m, '%b-%Y')
				target_dates.append({'year': dt.year, 'month': dt.month, 'label': m})
				# avoid duplicate labels
				if m not in period_labels:
					period_labels.append(m)
			except Exception:
				print(f"financial_reporting: failed parsing month '{m}'")

	# Handle quarter selections (e.g., 'Q1-2025' or 'Q1 2025') — expand to months
	if selected_quarters:
		for q in selected_quarters:
			m = re.match(r'Q([1-4])[-\s/_]?(\d{4})', q, re.I)
			if m:
				qnum = int(m.group(1))
				year = int(m.group(2))
				months_map = {1: [1, 2, 3], 2: [4, 5, 6], 3: [7, 8, 9], 4: [10, 11, 12]}
				for month in months_map[qnum]:
					label = datetime(year, month, 1).strftime('%b-%Y')
					entry = {'year': year, 'month': month, 'label': label}
					if entry not in target_dates:
						target_dates.append(entry)
					if label not in period_labels:
						period_labels.append(label)
			else:
				print(f"financial_reporting: failed parsing quarter '{q}'")

	# If previous_period_info wasn't set earlier (because months/quarters were added), compute it now
	if not previous_period_info and target_dates and period_labels:
		first_target = target_dates[0]
		first_date = datetime(first_target['year'], first_target['month'], 1)
		prev_date = first_date - relativedelta(months=1)
		prev_label = prev_date.strftime('%b-%Y')
		previous_period_info = {
			'year': prev_date.year,
			'month': prev_date.month,
			'label': f"_prev_{prev_label}"
		}

	# Debug: show selected periods and parsed targets
	print(f"financial_reporting: selected_months={selected_months}, selected_years={selected_years}, selected_quarters={selected_quarters}")
	print(f"financial_reporting: period_labels={period_labels}, target_dates={target_dates}, previous_period_info={previous_period_info}")
	
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
	# Debug: log number of fetched records
	print(f"financial_reporting: fetched {len(all_records)} records from CardDrillthrough")
	
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

	# Debug: log built community count
	print(f"financial_reporting: built community_data count={len(community_data)}")
	
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
	
	# Build a concise period indicator for the header (prefer years if selected)
	if selected_years:
		display_labels = selected_years
	elif selected_quarters:
		display_labels = selected_quarters
	elif selected_months:
		display_labels = selected_months
	else:
		display_labels = period_labels

	# Normalize to strings and truncate if too long
	display_labels = [str(x) for x in display_labels]
	max_show = 10
	if len(display_labels) > max_show:
		period_indicator = ', '.join(display_labels[:max_show]) + f', +{len(display_labels)-max_show} more'
	else:
		period_indicator = ', '.join(display_labels)

	# Tooltip/title should show the full list
	period_indicator_title = 'Currently viewing financial data for these periods: ' + (', '.join(display_labels) if display_labels else '')

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
		'period_indicator': period_indicator,
		'period_indicator_title': period_indicator_title,
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


def _find_column_by_keywords_excluding(columns, keywords, exclude=None):
	exclude_set = set(exclude or [])
	for col in columns:
		if not col or col in exclude_set:
			continue
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
	{'key': 'unit', 'label': 'Unit', 'exact': 'unit', 'keywords': ['unit']},
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
	{'key': 'type', 'label': 'Type', 'keywords': ['type']},
	{'key': 'site_unit_id', 'label': 'Site / Unit Id', 'keywords': ['site', 'unit', 'id']},
]

TOTAL_UNIT_METRIC_HINTS = {
	'resident_name': ['resident', 'name'],
	'ntv_flag': ['ntv'],
	'exposure_8_weeks': ['exposure', '8'],
	'unit_type': ['type'],
	'move_out_date': ['move', 'out'],
	'scheduled_move_in': ['scheduled', 'move', 'in'],
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
			# Value may already be formatted (e.g. "$1,771.00") coming from the view.
			# Avoid prefixing another dollar sign — normalize sensible cases.
			s = str(value).strip()
			# If value already contains a dollar sign, assume it's formatted correctly.
			if '$' in s:
				return s
			# Try stripping commas/non-breaking spaces and format if numeric-like
			try:
				s2 = s.replace(',', '').replace('\u00A0', '')
				# Handle parentheses negative format
				if s2.startswith('(') and s2.endswith(')'):
					s2 = '-' + s2[1:-1]
				num = float(s2)
				return f"${num:,.2f}"
			except Exception:
				# Fallback: return the raw string (no extra $ prefix)
				return s
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


def _get_trade_out_columns():
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
				['web_ai', 'trade_out_drill_through']
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Trade out drill-through column introspection failed:', exc)
	if columns:
		return columns
	try:
		with connection.cursor() as cur:
			cur.execute(
				"""
				SELECT attname
				FROM pg_attribute
				WHERE attrelid = 'web_ai.trade_out_drill_through'::regclass
				  AND attnum > 0
				  AND NOT attisdropped
				ORDER BY attnum
				"""
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Trade out drill-through pg_attribute introspection failed:', exc)
	return columns


def _fetch_total_unit_summary(filter_clauses, filter_params, metric_columns):
	summary = {
		'unit_count': 0,
		'occupancy_pct': None,
		'exposure_8_weeks': None,
		'vacant_units': None,
		'ntv_units': None,
		'exposure_units': None,
	}

	# Prefer counting distinct site/unit identifier to avoid duplicate rows
	# and to match dashboard KPI expectations. Fall back to COUNT(DISTINCT unit)
	# when no site identifier column is available.
	cols_for_count = _get_total_unit_drill_columns()
	site_id_col = _find_exact_column(cols_for_count, 'site_id_property_unit_number') or _find_exact_column(cols_for_count, 'OneSiteID-Property-Unit') or _find_column_by_keywords(cols_for_count, ['site', 'unit', 'id'])
	if site_id_col:
		sql_parts = [f"COUNT(DISTINCT {_quote_ident(site_id_col)}) AS total_units"]
	else:
		sql_parts = ["COUNT(DISTINCT unit) AS total_units"]
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
	# Always exclude Livcor properties from the Unit Count to be consistent
	# with dashboard KPI expectations. Add the investor exclusion clause
	# in a normalized (lower/trim) form.
	cols_for_count2 = cols_for_count
	inv_col = _find_column_by_keywords(cols_for_count2, ['investor'])
	local_clauses = list(filter_clauses) if filter_clauses else []
	local_params = list(filter_params) if filter_params else []
	if inv_col:
		inv_ident = _quote_ident(inv_col)
		local_clauses.append(f"LOWER(TRIM({inv_ident}::text)) NOT LIKE %s")
		local_params.append('%livcor%')
	if local_clauses:
		sql += ' WHERE ' + ' AND '.join(local_clauses)

	try:
		with connection.cursor() as cur:
			cur.execute(sql, local_params)
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

	dedup_columns = list(alias_to_source.values())
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
		'dedup_columns': dedup_columns,
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
		detail_url = ''
		unit_identifier = row_dict.get('site_unit_id') or row_dict.get('unit_identifier') or row_dict.get('unit')
		if unit_identifier:
			try:
				detail_path = reverse('total_unit_detail', kwargs={'unit_token': unit_identifier})
			except Exception:
				detail_path = ''
			if detail_path:
				query_pairs = []
				property_name_value = row_dict.get('property_name')
				if property_name_value:
					query_pairs.append(('property', property_name_value))
				return_param = request.get_full_path() if hasattr(request, 'get_full_path') else ''
				if return_param:
					query_pairs.append(('return_url', return_param))
				if query_pairs:
					detail_url = f"{detail_path}?{urlencode(query_pairs, doseq=True)}"
				else:
					detail_url = detail_path
		rows.append({'values': ordered_values, 'detail_url': detail_url})

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
		'total_count': total_units,
	}

	export_pairs = [(k, v) for (k, v) in base_query_pairs if k != 'return_url']
	# Do not include 'page' parameter — export should return the full filtered dataset
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
	
	# Add data quality filter: exclude extreme NOI outliers (>100% or <-100%)
	# that indicate data issues (negative/zero revenue). Normal NOI ranges 30-80%.
	noi_col = _find_column_by_keywords(columns, ['noi', 'revenue'])
	if noi_col:
		filter_clauses.append(f"({_quote_ident(noi_col)} BETWEEN -100 AND 100 OR {_quote_ident(noi_col)} IS NULL)")

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

	# Business rule: exclude BLACKSTONE/LIVCOR for periods after June 2025
	# unless explicitly selected by the user via investor filter
	if investor_col and month_starts:
		# Check if any selected month is after June 2025
		jun_2025 = date(2025, 6, 1)
		has_post_june = any(ms > jun_2025 for ms in month_starts)
		
		if has_post_june:
			# Check if LivCor is explicitly selected
			inv_values_upper = [str(v).upper() for v in inv_values] if inv_values else []
			livcor_selected = any(x in inv_values_upper for x in ['BLACKSTONE/LIVCOR', 'LIVCOR'])
			
			if not livcor_selected:
				# Exclude LivCor properties
				where_clauses.append(f"({_quote_ident(investor_col)} NOT ILIKE %s AND {_quote_ident(investor_col)} NOT ILIKE %s)")
				sql_params.extend(['%BLACKSTONE/LIVCOR%', '%LIVCOR%'])

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


def format_finance_kpi_values(raw_values, request=None, params=None, svc_ctx=None):
	"""Return display-ready strings for finance KPIs.

	DAX rule for NOI: return 'N/A' when the selected period includes the
	current month OR when the NOI value is blank/None.
	
	Note: All percentage values from finance_kpi_scorecard are already stored
	as percentages (e.g., 61.5 means 61.5%, not 0.615), so we do NOT multiply by 100.
	"""
	def fmt_percent(val):
		if val is None:
			return '--'
		try:
			val = float(val)
		except Exception:
			return '--'
		# Values are already stored as percentages, so no multiplication needed
		return f"{val:.2f}%"

	def fmt_currency(val):
		if val is None:
			return '--'
		try:
			val = float(val)
		except Exception:
			return '--'
		return f"${val:,.2f}"

	# Determine whether NOI should be displayed as 'N/A' per DAX rule.
	noi_raw = raw_values.get('noi_percent_revenue')
	noi_na = False
	if noi_raw is None:
		noi_na = True
	else:
		# If caller passed enough context, check whether selected period
		# includes the current month. If so, return 'N/A'.
		try:
			if request is not None and (params is not None or svc_ctx is not None):
				month_starts = _resolve_selected_month_starts(request, params, svc_ctx)
				if month_starts:
					today_month_start = date.today().replace(day=1)
					# If any selected month equals current month, hide NOI
					if any(ms == today_month_start for ms in month_starts):
						noi_na = True
		except Exception:
			# Be conservative: do not fail the whole formatting if resolution fails
			noi_na = noi_na or False

	return {
		'in_place_rent_per_sqft': fmt_currency(raw_values.get('in_place_rent_per_sqft')),
		'yoy_change_operating_revenue': fmt_percent(raw_values.get('yoy_change_operating_revenue')),
		'yoy_change_expense': fmt_percent(raw_values.get('yoy_change_expense')),
		'noi_percent_revenue': 'N/A' if noi_na else fmt_percent(raw_values.get('noi_percent_revenue')),
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

	from django.utils import timezone
	# Build SQL and fetch all filtered rows (no pagination)
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
		print('Occupancy drill-through export failed:', exc)
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
def card_transactions_analytics(request):
	"""Interactive analytics for corporate card spend."""
	from decimal import Decimal
	import json

	date_format = '%Y-%m-%d'
	default_end = timezone.now().date()
	default_start = default_end - timedelta(days=180)

	def parse_date(value, fallback):
		try:
			return datetime.strptime(value, date_format).date()
		except (TypeError, ValueError):
			return fallback

	start_date = parse_date(request.GET.get('start'), default_start)
	end_date = parse_date(request.GET.get('end'), default_end)

	if start_date and end_date and start_date > end_date:
		start_date, end_date = end_date, start_date

	transactions = CardTransactions.objects.annotate(
		effective_date=Coalesce('transaction_date', 'transaction_date_clean', 'post_date', output_field=DateField())
	)

	if start_date:
		transactions = transactions.filter(effective_date__gte=start_date)
	if end_date:
		transactions = transactions.filter(effective_date__lte=end_date)

	metrics = transactions.aggregate(
		total_spend=Coalesce(Sum('transaction_amount'), Decimal('0')),
		total_sales_tax=Coalesce(Sum('sales_tax'), Decimal('0')),
		avg_ticket=Avg('transaction_amount'),
		txn_count=Count('row_hash_key', distinct=True)
	)

	total_spend = metrics.get('total_spend') or Decimal('0')
	avg_ticket = metrics.get('avg_ticket') or Decimal('0')
	total_transactions = metrics.get('txn_count') or 0
	sales_tax_total = metrics.get('total_sales_tax') or Decimal('0')

	unique_merchants = transactions.filter(merchant_name__isnull=False).exclude(merchant_name='').values('merchant_name').distinct().count()
	unique_cardholders = transactions.filter(cardholder__isnull=False).exclude(cardholder='').values('cardholder').distinct().count()

	top_merchants_qs = transactions.filter(transaction_amount__isnull=False).values('merchant_name').annotate(
		total_spend=Sum('transaction_amount'),
		txn_count=Count('row_hash_key', distinct=True)
	).order_by('-total_spend')[:10]

	top_merchants = []
	for row in top_merchants_qs:
		merchant_name = row['merchant_name'] or 'Unspecified'
		merchant_total = row['total_spend'] or Decimal('0')
		share = float(merchant_total / total_spend * 100) if total_spend else 0
		top_merchants.append({
			'name': merchant_name,
			'total_spend': merchant_total,
			'txn_count': row['txn_count'],
			'share': share
		})

	top_cardholders_qs = transactions.filter(transaction_amount__isnull=False).values('cardholder').annotate(
		total_spend=Sum('transaction_amount'),
		txn_count=Count('row_hash_key', distinct=True)
	).order_by('-total_spend')[:10]

	top_cardholders = []
	for row in top_cardholders_qs:
		name = row['cardholder'] or 'Unspecified'
		spend = row['total_spend'] or Decimal('0')
		share = float(spend / total_spend * 100) if total_spend else 0
		top_cardholders.append({
			'name': name,
			'total_spend': spend,
			'txn_count': row['txn_count'],
			'share': share
		})

	category_spend_qs = transactions.filter(
		transaction_amount__isnull=False,
		mcc_description__isnull=False
	).exclude(mcc_description='').values('mcc_description').annotate(
		total_spend=Sum('transaction_amount')
	).order_by('-total_spend')[:8]

	category_spend = [{
		'label': row['mcc_description'],
		'value': round(float(row['total_spend'] or 0), 2)
	} for row in category_spend_qs]

	state_spend = [{
		'state': row['merchant_state_province'],
		'total_spend': row['total_spend']
	} for row in transactions.filter(merchant_state_province__isnull=False)
	.exclude(merchant_state_province='')
	.values('merchant_state_province')
	.annotate(total_spend=Sum('transaction_amount'))
	.order_by('-total_spend')[:8]]

	monthly_trend_qs = transactions.filter(effective_date__isnull=False).annotate(
		month=TruncMonth('effective_date')
	).values('month').annotate(
		total_spend=Sum('transaction_amount')
	).order_by('month')

	monthly_labels = []
	monthly_values = []
	for row in monthly_trend_qs:
		month = row['month']
		monthly_labels.append(month.strftime('%b %Y') if month else 'Unspecified')
		monthly_values.append(round(float(row['total_spend'] or 0), 2))

	merchant_chart_labels = [m['name'] for m in top_merchants[:5]]
	merchant_chart_values = [round(float(m['total_spend'] or 0), 2) for m in top_merchants[:5]]

	category_labels = [c['label'] for c in category_spend]
	category_values = [c['value'] for c in category_spend]

	monthly_chart = {
		'labels': monthly_labels,
		'datasets': [{
			'label': 'Total Spend',
			'data': monthly_values,
			'borderColor': '#0E555A',
			'backgroundColor': 'rgba(89, 230, 246, 0.35)',
			'tension': 0.35,
			'fill': True,
		}]
	}

	merchant_chart = {
		'labels': merchant_chart_labels,
		'datasets': [{
			'label': 'Top Merchants',
			'data': merchant_chart_values,
			'backgroundColor': '#59E6F6',
			'borderColor': '#0E555A',
			'borderWidth': 1.5,
		}]
	}

	category_chart = {
		'labels': category_labels,
		'datasets': [{
			'label': 'Spend by Category',
			'data': category_values,
			'backgroundColor': ['#59E6F6', '#C69A58', '#0E555A', '#95A3B3', '#7BD3EA', '#4F46E5', '#F59E0B', '#0EA5E9'][:len(category_values)],
		}]
	}

	transactions_table = list(
		transactions.order_by('-effective_date', '-transaction_amount')
		.values('effective_date', 'transaction_id', 'merchant_name', 'transaction_amount', 'cardholder', 'merchant_city', 'transaction_type')[:25]
	)

	range_days = max((end_date - start_date).days + 1, 1)
	daily_average = total_spend / range_days if range_days else Decimal('0')
	merchant_surcharge_total = transactions.aggregate(total=Coalesce(Sum('merchant_surcharge'), Decimal('0')))['total'] or Decimal('0')
	dispute_count = transactions.filter(dispute_indicator__iexact='Y').count()
	pending_approvals = transactions.filter(transaction_approval_status__iexact='Pending').count()
	international_total = transactions.exclude(merchant_country__in=['US', 'USA', 'United States', '', None]).aggregate(
		total=Coalesce(Sum('transaction_amount'), Decimal('0'))
	)['total'] or Decimal('0')

	context = {
		'total_spend': total_spend,
		'total_transactions': total_transactions,
		'avg_ticket': avg_ticket,
		'sales_tax_total': sales_tax_total,
		'unique_merchants': unique_merchants,
		'unique_cardholders': unique_cardholders,
		'top_merchants': top_merchants,
		'top_cardholders': top_cardholders,
		'category_spend': category_spend,
		'state_spend': state_spend,
		'monthly_chart_json': json.dumps(monthly_chart),
		'merchant_chart_json': json.dumps(merchant_chart),
		'category_chart_json': json.dumps(category_chart),
		'transactions_table': transactions_table,
		'start_date': start_date.strftime(date_format) if start_date else '',
		'end_date': end_date.strftime(date_format) if end_date else '',
		'daily_average': daily_average,
		'merchant_surcharge_total': merchant_surcharge_total,
		'dispute_count': dispute_count,
		'pending_approvals': pending_approvals,
		'international_total': international_total,
	}

	return render(request, 'dashboard/card_transactions.html', context)


@login_required
def total_unit_detail(request, unit_token):
	"""Render a unit-level detail page with lease history and related activity."""
	columns = _get_total_unit_drill_columns()
	return_url = request.GET.get('return_url') or reverse('total_units_drillthrough')
	unit_token = (unit_token or '').strip()
	if not columns or not unit_token:
		context = {
			'error': 'No unit identifier was provided or the dataset is unavailable.',
			'return_url': return_url,
			'page_title': 'Unit Detail',
		}
		return render(request, 'dashboard/unit_detail.html', context)

	property_filter = (request.GET.get('property') or '').strip()
	unit_id_col = _find_column_by_keywords(columns, ['site', 'unit', 'id']) or _find_column_by_keywords(columns, ['unit'])
	if not unit_id_col:
		context = {
			'error': 'The unit detail view could not locate the unit identifier column.',
			'return_url': return_url,
			'page_title': 'Unit Detail',
		}
		return render(request, 'dashboard/unit_detail.html', context)

	property_col = _find_column_by_keywords(columns, ['property', 'name'])

	select_specs = [
		{'key': 'property_name', 'keywords': ['property', 'name'], 'unique': True},
		{'key': 'community', 'keywords': ['community'], 'unique': True},
		{'key': 'unit_identifier', 'exact': 'unit_identifier', 'keywords': ['unit'], 'unique': True},
		{'key': 'site_unit_id', 'keywords': ['site', 'unit', 'id'], 'unique': True},
		{'key': 'floor_plan', 'keywords': ['floor', 'plan'], 'unique': True},
		{'key': 'beds_baths', 'keywords': ['bed', 'bath'], 'unique': True},
		{'key': 'unit_status', 'keywords': ['status'], 'unique': True},
		{'key': 'availability_date', 'keywords': ['available', 'date'], 'unique': True},
		{'key': 'move_in_date', 'keywords': ['move', 'in'], 'unique': True},
		{'key': 'lease_start', 'keywords': ['lease', 'start'], 'unique': True},
		{'key': 'lease_end', 'keywords': ['lease', 'end'], 'unique': True},
		{'key': 'lease_term', 'keywords': ['lease', 'term'], 'unique': True},
		{'key': 'previous_lease_term', 'keywords': ['previous', 'lease', 'term'], 'unique': True},
		{'key': 'resident_name', 'keywords': ['resident', 'name'], 'unique': True},
		{'key': 'effective_rent', 'keywords': ['effective', 'rent'], 'unique': True},
		{'key': 'market_rent', 'keywords': ['market', 'rent'], 'unique': True},
		{'key': 'occupancy_pct', 'keywords': ['occupancy'], 'unique': True},
		{'key': 'lease_id', 'keywords': ['lease', 'id'], 'unique': True},
		{'key': 'move_out_date', 'keywords': ['move', 'out'], 'unique': True},
		{'key': 'move_out_reason', 'keywords': ['move', 'out', 'reason'], 'unique': True},
		{'key': 'notice_for_date', 'keywords': ['notice'], 'unique': True},
		{'key': 'days_occupied', 'keywords': ['days', 'occupied'], 'unique': True},
		{'key': 'amenity_value', 'keywords': ['amenity', 'value'], 'unique': True},
	]

	used_columns = set()
	select_parts = []
	select_aliases = []
	for spec in select_specs:
		col = None
		exact = spec.get('exact')
		if exact:
			col = _find_exact_column(columns, exact)
		if not col and spec.get('keywords'):
			exclude = used_columns if spec.get('unique', True) else None
			col = _find_column_by_keywords_excluding(columns, spec['keywords'], exclude)
		if not col:
			continue
		alias = spec['key']
		select_aliases.append(alias)
		select_parts.append(f"{_quote_ident(col)} AS {alias}")
		if spec.get('unique', True):
			used_columns.add(col)

	if not select_parts:
		context = {
			'error': 'No readable columns were found for the unit detail view.',
			'return_url': return_url,
			'page_title': 'Unit Detail',
		}
		return render(request, 'dashboard/unit_detail.html', context)

	identifier_columns = []
	seen_columns = set()
	if unit_id_col:
		ident = _quote_ident(unit_id_col)
		identifier_columns.append(ident)
		seen_columns.add(unit_id_col)
	unit_alias_col = _find_column_by_keywords(columns, ['unit'])
	if unit_alias_col and unit_alias_col not in seen_columns:
		identifier_columns.append(_quote_ident(unit_alias_col))
		seen_columns.add(unit_alias_col)
	onesite_alias_col = _find_column_by_keywords(columns, ['onesite', 'property'])
	if onesite_alias_col and onesite_alias_col not in seen_columns:
		identifier_columns.append(_quote_ident(onesite_alias_col))

	token_candidates = []
	seen_tokens = set()
	for candidate in [unit_token, request.GET.get('fallback_site_id', ''), request.GET.get('fallback_onesite_id', ''), request.GET.get('fallback_unit', '')]:
		value = (candidate or '').strip()
		if value and value not in seen_tokens:
			token_candidates.append(value)
			seen_tokens.add(value)

	order_clause = ''
	if 'lease_start' in select_aliases:
		order_clause = ' ORDER BY lease_start DESC NULLS LAST'
	elif 'move_in_date' in select_aliases:
		order_clause = ' ORDER BY move_in_date DESC NULLS LAST'
	else:
		order_clause = f" ORDER BY {select_aliases[0]} ASC"

	unit_records = []
	for ident in identifier_columns:
		for token_value in token_candidates:
			where_parts = [f"{ident} = %s"]
			params = [token_value]
			if property_filter and property_col:
				where_parts.append(f"{_quote_ident(property_col)} = %s")
				params.append(property_filter)
			where_sql = ' WHERE ' + ' AND '.join(where_parts)
			data_sql = f"SELECT {', '.join(select_parts)} FROM {TOTAL_UNIT_DRILL_VIEW}{where_sql}{order_clause} LIMIT 200"
			try:
				with connection.cursor() as cur:
					cur.execute(data_sql, params)
					rows = cur.fetchall()
			except Exception as exc:
				print('Unit detail query failed:', exc)
				rows = []
			if rows:
				for raw in rows:
					unit_records.append(dict(zip(select_aliases, raw)))
				break
		if unit_records:
			break

	date_extra_fields = {
		'availability_date', 'notice_for_date', 'move_out_date', 'current_lease_start_date', 'current_lease_end_date'
	}
	currency_extra_fields = {
		'trade_out_dollar', 'current_lease_effective_rent', 'previous_lease_effective_rent'
	}
	percent_fields = {'occupancy_pct', 'trade_out_pct'}
	integer_fields = {'lease_term', 'previous_lease_term', 'days_occupied'}

	def format_value(key, value):
		if value is None:
			return '--'
		if key in TOTAL_UNIT_CURRENCY_FIELDS or key in TOTAL_UNIT_DATE_FIELDS:
			return _format_total_unit_value(key, value)
		if key in {'lease_term', 'previous_lease_term', 'resident_name', 'property_name', 'unit_identifier', 'site_unit_id', 'floor_plan', 'beds_baths', 'lease_id', 'move_out_reason', 'unit_status', 'community'}:
			return _format_total_unit_value(key, value)
		if isinstance(value, (datetime, date)):
			return value.strftime('%b %d, %Y')
		if key in date_extra_fields:
			s = str(value).strip()
			if not s:
				return '--'
			try:
				parsed = datetime.strptime(s[:10], '%Y-%m-%d').date()
				return parsed.strftime('%b %d, %Y')
			except Exception:
				return s
		if key in currency_extra_fields:
			s = str(value).strip()
			try:
				if isinstance(value, Decimal):
					num = float(value)
				else:
					s_clean = s.replace('$', '').replace(',', '').replace('\u00A0', '')
					if s_clean.startswith('(') and s_clean.endswith(')'):
						s_clean = '-' + s_clean[1:-1]
					num = float(s_clean)
			except Exception:
				return s or '--'
			return f"${num:,.2f}"
		if key in percent_fields:
			try:
				num = float(value)
				if abs(num) <= 1:
					num *= 100
				return f"{num:.1f}%"
			except Exception:
				s = str(value).strip()
				if s.endswith('%'):
					return s
				return s or '--'
		if key in integer_fields:
			try:
				num = int(round(float(value)))
				return str(num)
			except Exception:
				return str(value)
		s = str(value).strip()
		return s or '--'

	primary_row = unit_records[0] if unit_records else {}
	property_name_raw = primary_row.get('property_name') or property_filter
	property_name_filter = str(property_name_raw).strip() if property_name_raw else ''
	unit_identifier_raw = primary_row.get('unit_identifier') or primary_row.get('site_unit_id') or unit_token
	unit_identifier_filter = str(unit_identifier_raw).strip() if unit_identifier_raw else ''

	unit_profile = {
		'property_name': property_name_filter or '--',
		'community': primary_row.get('community') or '--',
		'unit_identifier': unit_identifier_filter or '--',
		'site_unit_id': primary_row.get('site_unit_id') or unit_token,
		'floor_plan': primary_row.get('floor_plan') or '--',
		'beds_baths': primary_row.get('beds_baths') or '--',
		'unit_status': format_value('unit_status', primary_row.get('unit_status')),
		'amenity_value': format_value('amenity_value', primary_row.get('amenity_value')),
		'resident_name': format_value('resident_name', primary_row.get('resident_name')),
		'availability_date': format_value('availability_date', primary_row.get('availability_date')),
		'lease_term': format_value('lease_term', primary_row.get('lease_term')),
		'occupancy_pct': format_value('occupancy_pct', primary_row.get('occupancy_pct')),
	}

	fallback_values = {
		'unit_identifier': (request.GET.get('fallback_unit') or '').strip(),
		'floor_plan': (request.GET.get('fallback_floor_plan') or '').strip(),
		'beds_baths': (request.GET.get('fallback_beds_baths') or '').strip(),
		'unit_status': (request.GET.get('fallback_unit_status') or '').strip(),
		'availability_date': (request.GET.get('fallback_availability_date') or '').strip(),
		'amenity_value': (request.GET.get('fallback_amenity_value') or '').strip(),
		'lease_term': (request.GET.get('fallback_lease_term') or '').strip(),
	}

	if unit_profile['unit_identifier'] in ('', '--'):
		if fallback_values['unit_identifier']:
			unit_profile['unit_identifier'] = fallback_values['unit_identifier']
		elif unit_token:
			unit_profile['unit_identifier'] = unit_token

	if unit_profile.get('site_unit_id') in ('', '--', None) and unit_profile['unit_identifier'] not in ('', '--'):
		unit_profile['site_unit_id'] = unit_profile['unit_identifier']

	if unit_profile['floor_plan'] in ('', '--') and fallback_values['floor_plan']:
		unit_profile['floor_plan'] = fallback_values['floor_plan']

	if unit_profile['beds_baths'] in ('', '--'):
		if fallback_values['beds_baths']:
			unit_profile['beds_baths'] = fallback_values['beds_baths']

	if unit_profile['unit_status'] in ('', '--') and fallback_values['unit_status']:
		unit_profile['unit_status'] = format_value('unit_status', fallback_values['unit_status'])

	if unit_profile['availability_date'] in ('', '--') and fallback_values['availability_date']:
		unit_profile['availability_date'] = format_value('availability_date', fallback_values['availability_date'])

	if unit_profile['amenity_value'] in ('', '--') and fallback_values['amenity_value']:
		unit_profile['amenity_value'] = format_value('amenity_value', fallback_values['amenity_value'])

	if unit_profile['lease_term'] in ('', '--') and fallback_values['lease_term']:
		unit_profile['lease_term'] = format_value('lease_term', fallback_values['lease_term'])

	if unit_profile['property_name'] in ('', '--') and property_filter:
		unit_profile['property_name'] = property_filter

	# Ensure we have a unit-level lookup identifier available for navigation and MV queries
	unit_level_lookup = unit_profile.get('site_unit_id') or unit_token

	prev_unit_token = None
	next_unit_token = None
	prev_unit_label = None
	next_unit_label = None
	unit_nav_identifier_col = _find_exact_column(columns, 'unit_identifier') or _find_column_by_keywords(columns, ['unit', 'identifier'])
	unit_nav_list = []
	if property_name_filter and property_col and unit_level_lookup:
		unit_token_ident = _quote_ident(unit_id_col)
		unit_label_ident = _quote_ident(unit_nav_identifier_col if unit_nav_identifier_col else unit_id_col)
		property_ident = _quote_ident(property_col)
		nav_sql = (
			f"SELECT DISTINCT {unit_token_ident} AS unit_token, {unit_label_ident} AS unit_label "
			f"FROM {TOTAL_UNIT_DRILL_VIEW} "
			f"WHERE {property_ident} = %s "
			f"AND {unit_token_ident} IS NOT NULL "
			f"ORDER BY unit_label ASC LIMIT 1000"
		)
		try:
			with connection.cursor() as cur:
				cur.execute(nav_sql, [property_name_filter])
				nav_rows = cur.fetchall()
		except Exception as exc:
			print('Unit navigation query failed:', exc)
			nav_rows = []
		for row in nav_rows:
			token_raw = row[0] if len(row) > 0 else None
			label_raw = row[1] if len(row) > 1 else token_raw
			if token_raw in (None, ''):
				continue
			token_str = str(token_raw).strip()
			if not token_str:
				continue
			label_str = str(label_raw).strip() if label_raw not in (None, '') else token_str
			unit_nav_list.append((token_str, label_str))
		current_token = str(unit_level_lookup or '').strip()
		for idx, (token_str, label_str) in enumerate(unit_nav_list):
			if token_str == current_token:
				if idx > 0:
					prev_unit_token, prev_unit_label = unit_nav_list[idx - 1]
				if idx < len(unit_nav_list) - 1:
					next_unit_token, next_unit_label = unit_nav_list[idx + 1]
				break

	renewal_count = 0
	new_lease_count = 0
	latest_trade_out_value = None
	history_column_specs = [
		{
			'label': 'Lease ID',
			'mv_keys': ['lease_id'],
			'trade_keys': ['lease_id'],
			'total_keys': ['lease_id'],
		},
		{
			'label': 'Move-in Date',
			'mv_keys': ['move_in_date'],
			'trade_keys': ['move_in_date'],
			'total_keys': ['move_in_date'],
		},
		{
			'label': 'Lease Start Date',
			'mv_keys': ['lease_start_date', 'effective_lease_start_date'],
			'trade_keys': ['current_lease_start_date', 'lease_start_date'],
			'total_keys': ['lease_start'],
		},
		{
			'label': 'Lease End Date',
			'mv_keys': ['actual_lease_end', 'scheduled_lease_end'],
			'trade_keys': ['current_lease_end_date', 'lease_end', 'actual_lease_end'],
			'total_keys': ['lease_end'],
		},
		{
			'label': 'Lease Term',
			'mv_keys': ['lease_term'],
			'trade_keys': ['current_lease_term', 'lease_term'],
			'total_keys': ['lease_term'],
		},
		{
			'label': 'Previous Lease Term',
			'mv_keys': ['previous_term', 'previous_lease_term'],
			'trade_keys': ['previous_lease_term', 'previous_term'],
			'total_keys': ['previous_lease_term'],
		},
		{
			'label': 'Lease Effective Rent',
			'mv_keys': ['effective_rent'],
			'trade_keys': ['current_lease_effective_rent', 'effective_rent'],
			'total_keys': ['effective_rent'],
		},
		{
			'label': 'Previous Lease Effective Rent',
			'mv_keys': ['previous_lease_effective_rent'],
			'trade_keys': ['previous_lease_effective_rent'],
			'total_keys': ['previous_lease_effective_rent'],
		},
		{
			'label': 'Trade Out $',
			'mv_keys': ['trade_out_dollars'],
			'trade_keys': ['trade_out_dollar', 'trade_out_dollars'],
			'total_keys': ['trade_out_dollar'],
		},
		{
			'label': 'Trade Out %',
			'mv_keys': ['trade_out_pct'],
			'trade_keys': ['trade_out_pct'],
			'total_keys': ['trade_out_pct'],
		},
		{
			'label': 'Notice For Date',
			'mv_keys': ['move_out_notice_date'],
			'trade_keys': ['notice_for_date', 'move_out_notice_date'],
			'total_keys': ['notice_for_date'],
		},
		{
			'label': 'Moved Out Date',
			'mv_keys': ['actual_move_out_date'],
			'trade_keys': ['move_out_date', 'actual_move_out_date'],
			'total_keys': ['move_out_date'],
		},
		{
			'label': 'Lease Type',
			'mv_keys': ['rate_type'],
			'trade_keys': ['renewal_new_lease', 'rate_type'],
			'total_keys': ['lease_type', 'rate_type'],
		},
		{
			'label': 'Move Out Reason',
			'mv_keys': ['move_out_reason'],
			'trade_keys': ['move_out_reason'],
			'total_keys': ['move_out_reason'],
		},
		{
			'label': 'Days occupied',
			'mv_keys': ['days_occupied', 'effective_days_occupied'],
			'trade_keys': ['days_occupied'],
			'total_keys': ['days_occupied'],
		},
		{
			'label': 'Is Employee Lease',
			'mv_keys': ['is_employee_lease'],
			'trade_keys': ['is_employee_lease'],
			'total_keys': ['is_employee_lease'],
		},
	]
	history_columns = [{'key': spec['label'], 'label': spec['label']} for spec in history_column_specs]
	history_rows = []
	amenity_rows = []
	amenity_total_display = '--'
	amenity_columns = [
		{'key': 'amenity_type', 'label': 'Amenity Type'},
		{'key': 'amenity', 'label': 'Amenity'},
		{'key': 'amenity_level', 'label': 'Amenity Level'},
		{'key': 'amenity_cost', 'label': 'Amenity Cost'},
	]
	service_request_columns = []
	service_request_rows = []
	effective_days_occupied_total = 0.0
	total_days_since_unit_acquired_value = None
	occupancy_lease_ids = set()

	unit_level_mv_records = []
	if unit_level_lookup:
		try:
			unit_level_mv_records = list(UnitLevelDrillThrough.objects.filter(site_id_property_unit_number=unit_level_lookup))
		except Exception as exc:
			print('Unit level drill query failed:', exc)
			unit_level_mv_records = []

	if unit_level_mv_records:
		mv_currency_fields = {'effective_rent', 'previous_lease_effective_rent', 'trade_out_dollars', 'amenity_cost'}
		mv_percent_fields = {'trade_out_pct'}
		mv_date_fields = {
			'move_in_date', 'lease_start_date', 'effective_lease_start_date', 'scheduled_lease_end',
			'actual_lease_end', 'move_out_notice_date', 'actual_move_out_date'
		}
		mv_datetime_fields = {'created_date_time', 'completed_date_time'}
		mv_bool_fields = {'is_employee_lease'}

		def format_unit_level_value(key, value):
			if value is None:
				return '--'
			if key in mv_date_fields:
				if isinstance(value, (datetime, date)):
					return value.strftime('%b %d, %Y')
				if isinstance(value, str):
					s = value.strip()
					if not s:
						return '--'
					for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S'):
						try:
							parsed = datetime.strptime(s[:len(fmt)], fmt)
							return parsed.strftime('%b %d, %Y')
						except Exception:
							continue
					return s
			if key in mv_datetime_fields:
				if isinstance(value, datetime):
					return value.strftime('%b %d, %Y %I:%M %p')
				if isinstance(value, date):
					return value.strftime('%b %d, %Y')
				if isinstance(value, str):
					s = value.strip()
					if not s:
						return '--'
					for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
						try:
							parsed = datetime.strptime(s[:len(fmt)], fmt)
							return parsed.strftime('%b %d, %Y %I:%M %p')
						except Exception:
							continue
					return s
			if key in mv_currency_fields:
				try:
					if isinstance(value, Decimal):
						amount = float(value)
					else:
						s = str(value).strip()
						s_clean = s.replace('$', '').replace(',', '').replace('\u00A0', '')
						if s_clean.startswith('(') and s_clean.endswith(')'):
							s_clean = '-' + s_clean[1:-1]
						amount = float(s_clean)
					return f"${amount:,.2f}"
				except Exception:
					s = str(value).strip()
					return s or '--'
			if key in mv_percent_fields:
				try:
					num = float(value)
					if abs(num) <= 1:
						num *= 100
					return f"{num:.2f}%"
				except Exception:
					s = str(value).strip()
					if s.endswith('%'):
						return s
					return s or '--'
			if key in mv_bool_fields:
				return 'Yes' if bool(value) else 'No'
			if isinstance(value, (datetime, date)):
				return value.strftime('%b %d, %Y')
			s = str(value).strip()
			return s or '--'

		amenity_map = OrderedDict()
		service_request_map = {}
		history_map = OrderedDict()

		for record in unit_level_mv_records:
			if record is None:
				continue
			lease_identifier_raw = getattr(record, 'lease_id', None)
			lease_identifier = str(lease_identifier_raw or '').strip()
			has_lease = bool(lease_identifier)
			if has_lease and lease_identifier not in occupancy_lease_ids:
				occupancy_lease_ids.add(lease_identifier)
				eff_days_val = getattr(record, 'effective_days_occupied', None)
				if eff_days_val not in (None, ''):
					try:
						effective_days_occupied_total += float(eff_days_val)
					except Exception:
						pass
				total_days_val = getattr(record, 'total_days_since_unit_acquired', None)
				if total_days_val not in (None, ''):
					try:
						total_candidate = float(total_days_val)
						if total_candidate > 0:
							if total_days_since_unit_acquired_value is None or total_candidate > total_days_since_unit_acquired_value:
								total_days_since_unit_acquired_value = total_candidate
					except Exception:
						pass
			# Amenity aggregation
			if record.amenity or record.amenity_type or record.amenity_level or record.amenity_cost is not None:
				amenity_key = (
					(str(record.amenity_type or '').strip()),
					(str(record.amenity or '').strip()),
					(str(record.amenity_level or '').strip()),
				)
				entry = amenity_map.get(amenity_key)
				if not entry:
					entry = {
						'amenity_type': record.amenity_type,
						'amenity': record.amenity,
						'amenity_level': record.amenity_level,
						'amenity_cost': record.amenity_cost,
					}
					amenity_map[amenity_key] = entry
				else:
					for field in ('amenity_type', 'amenity', 'amenity_level', 'amenity_cost'):
						value = getattr(record, field, None)
						if field == 'amenity_cost':
							if entry.get(field) is None and value is not None:
								entry[field] = value
						else:
							if (entry.get(field) in (None, '') and value not in (None, '')):
								entry[field] = value

			# Service request aggregation
			if record.request_number:
				sr_entry = service_request_map.get(record.request_number)
				if not sr_entry:
					sr_entry = {
						'request_number': record.request_number,
						'item': record.item,
						'created_date_time': record.created_date_time,
						'completed_date_time': record.completed_date_time,
						'status': record.status,
					}
					service_request_map[record.request_number] = sr_entry
				else:
					for field in ('item', 'created_date_time', 'completed_date_time', 'status'):
						value = getattr(record, field, None)
						if sr_entry.get(field) in (None, '') and value not in (None, ''):
							sr_entry[field] = value

			# Lease history aggregation
			lease_key = (
				record.lease_id or '',
				record.lease_start_date,
				record.move_in_date,
				record.actual_lease_end,
			)
			if any(lease_key):
				history_entry = history_map.get(lease_key)
				if not history_entry:
					history_entry = {}
					history_map[lease_key] = history_entry
				for field in (
					'lease_id', 'move_in_date', 'lease_start_date', 'effective_lease_start_date',
					'scheduled_lease_end', 'actual_lease_end', 'rate_type', 'move_out_notice_date',
					'actual_move_out_date', 'move_out_reason', 'days_occupied', 'effective_days_occupied',
					'total_days_since_unit_acquired', 'is_employee_lease', 'effective_rent',
					'previous_lease_effective_rent', 'trade_out_dollars', 'trade_out_pct',
					'lease_term', 'previous_term'
				):
					value = getattr(record, field, None)
					if history_entry.get(field) in (None, '') and value not in (None, ''):
						history_entry[field] = value

		# Build amenity rows and totals
		if amenity_map:
			amenity_total_raw = 0.0
			for entry in amenity_map.values():
				cost_val = entry.get('amenity_cost')
				if cost_val not in (None, ''):
					try:
						amenity_total_raw += float(cost_val)
					except Exception:
						pass
				amenity_rows.append([
					format_unit_level_value('amenity_type', entry.get('amenity_type')),
					format_unit_level_value('amenity', entry.get('amenity')),
					format_unit_level_value('amenity_level', entry.get('amenity_level')),
					format_unit_level_value('amenity_cost', entry.get('amenity_cost')),
				])
			amenity_total_display = format_unit_level_value('amenity_cost', amenity_total_raw)

		# Build service request rows
		if service_request_map:
			service_request_columns = [
				{'key': 'request_number', 'label': 'Request Number'},
				{'key': 'item', 'label': 'Item'},
				{'key': 'created_date_time', 'label': 'Created Date'},
				{'key': 'completed_date_time', 'label': 'Completed Date'},
				{'key': 'status', 'label': 'Status'},
			]
			def _sr_sort(entry):
				val = entry.get('created_date_time')
				if isinstance(val, datetime):
					return val
				if isinstance(val, date):
					return datetime.combine(val, datetime.min.time())
				return datetime.min
			for entry in sorted(service_request_map.values(), key=_sr_sort, reverse=True):
				service_request_rows.append([
					entry.get('request_number') or '--',
					format_unit_level_value('item', entry.get('item')),
					format_unit_level_value('created_date_time', entry.get('created_date_time')),
					format_unit_level_value('completed_date_time', entry.get('completed_date_time')),
					format_unit_level_value('status', entry.get('status')),
				])

		# Build history rows from MV
		if history_map:
			def _history_sort(entry):
				val = entry.get('lease_start_date') or entry.get('move_in_date') or entry.get('effective_lease_start_date')
				if isinstance(val, (datetime, date)):
					return val
				if isinstance(val, str):
					try:
						return datetime.strptime(val[:10], '%Y-%m-%d')
					except Exception:
						return datetime.min
				return datetime.min

			for entry in sorted(history_map.values(), key=_history_sort, reverse=True):
				rate_lower = str(entry.get('rate_type') or '').lower()
				if 'renewal' in rate_lower:
					renewal_count += 1
				elif 'new' in rate_lower:
					new_lease_count += 1
				if latest_trade_out_value is None and entry.get('trade_out_dollars') is not None:
					try:
						latest_trade_out_value = float(entry['trade_out_dollars'])
					except Exception:
						latest_trade_out_value = entry['trade_out_dollars']
				row_values = []
				for spec in history_column_specs:
					value_key = None
					value = None
					for candidate in spec['mv_keys']:
						candidate_value = entry.get(candidate)
						if candidate_value not in (None, ''):
							value_key = candidate
							value = candidate_value
							break
					if value_key is None:
						row_values.append('--')
					else:
						row_values.append(format_unit_level_value(value_key, value))
				history_rows.append(row_values)

		primary_level_info = unit_level_mv_records[0]
		if unit_profile['unit_identifier'] in ('', '--'):
			alt_identifier = (
				getattr(primary_level_info, 'site_id_property_unit_number', None)
				or getattr(primary_level_info, 'unit_number', None)
			)
			if alt_identifier:
				resolved = str(alt_identifier).strip()
				if resolved:
					unit_profile['unit_identifier'] = resolved
					if unit_profile.get('site_unit_id') in ('', '--', None):
						unit_profile['site_unit_id'] = resolved
		if unit_profile['beds_baths'] in ('', '--'):
			alt_beds = getattr(primary_level_info, 'bedrooms_bathrooms', None)
			if alt_beds not in (None, ''):
				unit_profile['beds_baths'] = str(alt_beds).strip()
		if unit_profile['resident_name'] in ('', '--'):
			alt_resident = getattr(primary_level_info, 'resident_name', None)
			if alt_resident not in (None, ''):
				unit_profile['resident_name'] = str(alt_resident).strip()

	trade_columns = _get_trade_out_columns()
	trade_rows = []
	trade_aliases = []
	trade_chart = {'labels': [], 'values': []}
	trade_renewal_count = 0
	trade_new_lease_count = 0

	if trade_columns and unit_identifier_filter:
		trade_specs = [
			{'key': 'lease_id', 'exact': 'Lease ID'},
			{'key': 'move_in_date', 'exact': 'Move-In Date'},
			{'key': 'current_lease_start_date', 'exact': 'Current Lease Start Date'},
			{'key': 'current_lease_end_date', 'exact': 'Current Lease End Date'},
			{'key': 'trade_out_dollar', 'exact': 'Trade Out $', 'numeric': True},
			{'key': 'trade_out_pct', 'exact': 'Trade Out %', 'numeric': True, 'keywords': ['trade', 'percent']},
			{'key': 'current_lease_effective_rent', 'exact': 'Current Lease Effective Rent', 'numeric': True},
			{'key': 'previous_lease_effective_rent', 'exact': 'Previous Lease Effective Rent', 'numeric': True},
			{'key': 'current_lease_term', 'exact': 'Current Lease Term'},
			{'key': 'previous_lease_term', 'exact': 'Previous Lease Term'},
			{'key': 'renewal_new_lease', 'exact': 'Renewal/New Lease'},
			{'key': 'notice_for_date', 'exact': 'Notice For Date'},
			{'key': 'move_out_date', 'exact': 'Move-Out Date'},
			{'key': 'move_out_reason', 'exact': 'Move-Out Reason'},
			{'key': 'days_occupied', 'exact': 'Days Occupied'},
		]
		trade_select_parts = []
		trade_aliases = []
		for spec in trade_specs:
			col = None
			if spec.get('exact'):
				col = _find_exact_column(trade_columns, spec['exact'])
			if not col and spec.get('keywords'):
				col = _find_column_by_keywords(trade_columns, spec['keywords'])
			if not col:
				continue
			alias = spec['key']
			if spec.get('numeric'):
				expr = _clean_numeric_expr(col)
				trade_select_parts.append(f"{expr} AS {alias}")
			else:
				trade_select_parts.append(f"{_quote_ident(col)} AS {alias}")
			trade_aliases.append(alias)
		unit_col = _find_exact_column(trade_columns, 'unit') or _find_column_by_keywords(trade_columns, ['unit'])
		property_col_trade = _find_exact_column(trade_columns, 'property_name') or _find_column_by_keywords(trade_columns, ['property', 'name'])
		if trade_select_parts and unit_col:
			where_filters = [f"{_quote_ident(unit_col)} = %s"]
			trade_params = [unit_identifier_filter]
			if property_name_filter and property_col_trade:
				where_filters.append(f"{_quote_ident(property_col_trade)} = %s")
				trade_params.append(property_name_filter)
			where_clause = ' WHERE ' + ' AND '.join(where_filters)
			order_field = 'current_lease_start_date' if 'current_lease_start_date' in trade_aliases else trade_aliases[0]
			trade_sql = f"SELECT {', '.join(trade_select_parts)} FROM web_ai.trade_out_drill_through{where_clause} ORDER BY {order_field} ASC LIMIT 300"
			try:
				with connection.cursor() as cur:
					cur.execute(trade_sql, trade_params)
					trade_raw = cur.fetchall()
			except Exception as exc:
				print('Unit detail trade-out query failed:', exc)
				trade_raw = []
			for raw in trade_raw:
				row = dict(zip(trade_aliases, raw))
				trade_rows.append(row)
				start_date = row.get('current_lease_start_date')
				parsed_date = None
				if isinstance(start_date, (datetime, date)):
					parsed_date = start_date
				elif isinstance(start_date, str):
					try:
						parsed_date = datetime.strptime(start_date[:10], '%Y-%m-%d').date()
					except Exception:
						parsed_date = None
				if parsed_date:
					value = row.get('trade_out_dollar')
					try:
						float_val = float(value) if value is not None else 0.0
					except Exception:
						float_val = 0.0
					trade_chart['labels'].append(parsed_date.strftime('%b %Y'))
					trade_chart['values'].append(float_val)
				if row.get('trade_out_dollar') is not None:
					try:
						latest_trade_out_value = float(row.get('trade_out_dollar'))
					except Exception:
						pass
				type_val = str(row.get('renewal_new_lease') or '').lower()
				if 'renewal' in type_val:
					trade_renewal_count += 1
				elif 'new' in type_val:
					trade_new_lease_count += 1

		if renewal_count == 0 and new_lease_count == 0:
			renewal_count = trade_renewal_count
			new_lease_count = trade_new_lease_count

	if trade_rows:
		def _trade_sort(row):
			val = row.get('current_lease_start_date') or row.get('lease_start')
			if isinstance(val, (datetime, date)):
				return val
			if isinstance(val, str):
				try:
					return datetime.strptime(val[:10], '%Y-%m-%d')
				except Exception:
					return datetime.min
			return datetime.min
		for row in sorted(trade_rows, key=_trade_sort, reverse=True):
			row_values = []
			for spec in history_column_specs:
				value_key = None
				value = None
				for candidate in spec['trade_keys']:
					if candidate in row and row[candidate] not in (None, ''):
						value_key = candidate
						value = row[candidate]
						break
				if value_key is None:
					row_values.append('--')
				else:
					row_values.append(format_value(value_key, value))
			history_rows.append(row_values)
	elif unit_records:
		for row in unit_records:
			row_values = []
			for spec in history_column_specs:
				value_key = None
				value = None
				for candidate in spec['total_keys']:
					if candidate in row and row[candidate] not in (None, ''):
						value_key = candidate
						value = row[candidate]
						break
				if value_key is None:
					row_values.append('--')
				else:
					row_values.append(format_value(value_key, value))
			history_rows.append(row_values)

	if history_rows:
		history_rows = [row for row in history_rows if row and str(row[0]).strip() not in ('', '--')]

	if not service_request_rows:
		sr_columns = _get_service_request_drill_columns()
		if sr_columns and unit_identifier_filter:
			sr_specs = [
				{'key': 'request_number', 'label': 'Request #', 'keywords': ['request', 'number']},
				{'key': 'item', 'label': 'Item', 'keywords': ['item']},
				{'key': 'category', 'label': 'Category', 'keywords': ['category']},
				{'key': 'status', 'label': 'Status', 'keywords': ['status']},
				{'key': 'created_date', 'label': 'Created', 'keywords': ['created', 'date']},
				{'key': 'completed_date_time', 'label': 'Completed', 'keywords': ['completed', 'date']},
			]
			used_sr_cols = set()
			sr_select_parts = []
			sr_aliases = []
			sr_headers = []
			for spec in sr_specs:
				col = _find_column_by_keywords_excluding(sr_columns, spec.get('keywords', []), used_sr_cols)
				if not col and spec.get('exact'):
					col = _find_exact_column(sr_columns, spec['exact'])
				if not col:
					continue
				used_sr_cols.add(col)
				sr_select_parts.append(f"{_quote_ident(col)} AS {spec['key']}")
				sr_aliases.append(spec['key'])
				sr_headers.append({'key': spec['key'], 'label': spec['label']})
			unit_col_sr = _find_column_by_keywords(sr_columns, ['unit', 'number']) or _find_column_by_keywords(sr_columns, ['unit'])
			property_col_sr = _find_column_by_keywords(sr_columns, ['property', 'name']) or _find_column_by_keywords(sr_columns, ['community'])
			if sr_select_parts and unit_col_sr:
				where_segments = [f"{_quote_ident(unit_col_sr)} = %s"]
				sr_params = [unit_identifier_filter]
				if property_name_filter and property_col_sr:
					where_segments.append(f"{_quote_ident(property_col_sr)} = %s")
					sr_params.append(property_name_filter)
				where_clause_sr = ' WHERE ' + ' AND '.join(where_segments)
				order_field_sr = 'created_date' if 'created_date' in sr_aliases else sr_aliases[0]
				sr_sql = f"SELECT {', '.join(sr_select_parts)} FROM {SERVICE_REQUEST_DRILL_VIEW}{where_clause_sr} ORDER BY {order_field_sr} DESC NULLS LAST LIMIT 50"
				try:
					with connection.cursor() as cur:
						cur.execute(sr_sql, sr_params)
						sr_raw = cur.fetchall()
				except Exception as exc:
					print('Unit detail service request query failed:', exc)
					sr_raw = []
				for raw in sr_raw:
					row = dict(zip(sr_aliases, raw))
					service_request_rows.append([_format_service_request_value(alias, row.get(alias)) for alias in sr_aliases])
				service_request_columns = sr_headers

	availability_display = unit_profile.get('availability_date')
	if not availability_display or availability_display == '--':
		availability_display = timezone.localdate().strftime('%b %d, %Y')

	average_occupancy_display = '--'
	if total_days_since_unit_acquired_value and total_days_since_unit_acquired_value > 0:
		# Mirror the Power BI measure: SUM(effective_days_occupied) / total_days_since_unit_acquired
		ratio = effective_days_occupied_total / total_days_since_unit_acquired_value
		ratio = max(0.0, min(ratio, 1.0))
		average_occupancy_display = f"{ratio * 100:.1f}%"
	elif primary_row:
		average_occupancy_display = format_value('occupancy_pct', primary_row.get('occupancy_pct'))

	metrics = {
		'availability_as_of': availability_display,
		'average_occupancy': average_occupancy_display,
		'new_leases_total': str(new_lease_count),
		'renewal_total': str(renewal_count),
		'latest_trade_out': format_value('trade_out_dollar', latest_trade_out_value) if latest_trade_out_value is not None else '--',
		'beds_baths': unit_profile.get('beds_baths') or '--',
	}

	trade_chart_payload = {
		'labels': trade_chart['labels'],
		'values': [round(float(v), 2) for v in trade_chart['values']] if trade_chart['values'] else [],
	}
	has_trade_chart_data = bool(trade_chart_payload['values'])

	context = {
		'page_title': 'Unit Detail',
		'unit_profile': unit_profile,
		'unit_metrics': metrics,
		'amenity_columns': amenity_columns,
		'amenity_rows': amenity_rows,
		'amenity_total_display': amenity_total_display,
		'history_columns': history_columns,
		'history_rows': history_rows,
		'service_request_columns': service_request_columns,
		'service_request_rows': service_request_rows,
		'trade_chart_json': json.dumps(trade_chart_payload),
		'has_trade_chart_data': has_trade_chart_data,
		'property_name': unit_profile.get('property_name'),
		'unit_identifier': unit_profile.get('unit_identifier'),
		'property_filter_param': property_name_filter,
		'prev_unit_token': prev_unit_token,
		'prev_unit_label': prev_unit_label,
		'next_unit_token': next_unit_token,
		'next_unit_label': next_unit_label,
		'return_url': return_url,
		'error': '' if (unit_records or unit_level_mv_records) else 'No unit records were found for the selected filters.',
	}

	return render(request, 'dashboard/unit_detail.html', context)


# ============================================================================
# OCCUPANCY DRILL-THROUGH VIEWS
# ============================================================================

OCCUPANCY_DRILL_VIEW = 'web_ai.total_unit_occupancy_drill_through'
OCCUPANCY_DRILL_PAGE_SIZE = 500

OCCUPANCY_FILTER_FIELDS = [
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'keywords': ['regional', 'vp']},
	{'key': 'regional_manager', 'label': 'Regional Manager', 'keywords': ['regional', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
	{'key': 'floor_plan', 'label': 'Floor Plan', 'keywords': ['floor', 'plan']},
]

OCCUPANCY_TABLE_FIELDS = [
	{'key': 'property_name', 'label': 'Property Name', 'keywords': ['property', 'name']},
	{'key': 'unit_condition', 'label': 'Unit Condition', 'keywords': ['unit', 'condition']},
	{'key': 'type', 'label': 'Type', 'keywords': ['type']},
	{'key': 'unit', 'label': 'Unit', 'exact': 'unit', 'keywords': ['unit']},
	{'key': 'floor_plan', 'label': 'Floor Plan', 'keywords': ['floor', 'plan']},
	{'key': 'beds_baths', 'label': 'Beds / Baths', 'keywords': ['bed', 'bath']},
	{'key': 'floor_level', 'label': 'Floor Level', 'keywords': ['floor', 'level']},
	{'key': 'amenity_value', 'label': 'Amenity Value', 'keywords': ['amenity', 'value']},
	{'key': 'move_out', 'label': 'Move Out', 'keywords': ['move', 'out']},
	{'key': 'date_unit_available', 'label': 'Date Unit Available', 'keywords': ['available', 'date']},
	{'key': 'turn_time', 'label': 'Turn Time', 'keywords': ['turn', 'time']},
	{'key': 'status', 'label': 'Status', 'keywords': ['status']},
	{'key': 'not_ready_past_dates', 'label': 'Not Ready / Past Dates', 'keywords': ['not', 'ready', 'past']},
	{'key': 'mr_day_variance', 'label': 'MR Day Variance', 'keywords': ['variance', 'mr']},
	{'key': 'leased_not_leased', 'label': 'Leased/Not Leased', 'keywords': ['leased']},
	{'key': 'days_on_market', 'label': 'Days on Market', 'keywords': ['days', 'market']},
	{'key': 'market_rent', 'label': 'Market Rent', 'keywords': ['market', 'rent']},
	{'key': 'lease_rent', 'label': 'Lease Rent', 'keywords': ['lease', 'rent']},
	{'key': 'lease_term', 'label': 'Lease Term', 'keywords': ['lease', 'term']},
	{'key': '12_month_price', 'label': '12 Month Price', 'keywords': ['12', 'month', 'price']},
	{'key': 'best_term', 'label': 'Best Term', 'keywords': ['best', 'term']},
	{'key': 'best_price', 'label': 'Best Price', 'keywords': ['best', 'price']},
	{'key': 'forecasted_trade_out', 'label': 'Forecasted Trade out', 'keywords': ['forecasted', 'trade']},
	{'key': 'days_until_vacant', 'label': 'Days Until Vacant', 'keywords': ['days', 'until', 'vacant']},
	{'key': 'move_out_reason', 'label': 'Move out Reason', 'keywords': ['move', 'out', 'reason']},
	{'key': 'onesite_id', 'label': 'OneSiteID | Property # | Unit #', 'keywords': ['onesite', 'id']},
]

OCCUPANCY_CURRENCY_FIELDS = {'amenity_value', 'market_rent', 'lease_rent', '12_month_price', 'best_price', 'forecasted_trade_out'}
OCCUPANCY_DATE_FIELDS = {'move_out', 'date_unit_available'}


def _get_occupancy_drill_columns():
	"""Get all columns from the occupancy drill-through materialized view."""
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
		print('Occupancy drill-through column introspection failed:', exc)

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
		print('Occupancy drill-through pg_attribute introspection failed:', exc)
	return columns


def _format_occupancy_value(alias, value):
	"""Format occupancy drill-through values for display."""
	if value is None:
		return '--'
	if isinstance(value, (datetime, date)):
		return value.strftime('%b %d, %Y')
	if isinstance(value, Decimal):
		value = float(value)
	if alias in OCCUPANCY_CURRENCY_FIELDS:
		try:
			return f"${float(value):,.2f}"
		except Exception:
			# Avoid double-dollar when DB already returns formatted string
			s = str(value).strip()
			if '$' in s:
				return s
			try:
				s2 = s.replace(',', '').replace('\u00A0', '')
				if s2.startswith('(') and s2.endswith(')'):
					s2 = '-' + s2[1:-1]
				num = float(s2)
				return f"${num:,.2f}"
			except Exception:
				return s
	if alias in OCCUPANCY_DATE_FIELDS:
		try:
			parsed = datetime.strptime(str(value), '%Y-%m-%d').date()
			return parsed.strftime('%b %d, %Y')
		except Exception:
			return str(value)
	text = str(value).strip()
	return text or '--'


def _fetch_occupancy_filter_options(columns):
	"""Fetch distinct filter options for occupancy drill-through filters."""
	options = {}
	
	# Find unit_condition (snake_case) column for base filtering
	unit_condition_col = _find_exact_column(columns, 'unit_condition')
	if not unit_condition_col:
		unit_condition_col = _find_column_by_keywords(columns, ['unit', 'condition'])
	
	unit_condition_filter = ''
	if unit_condition_col:
		unit_condition_ident = _quote_ident(unit_condition_col)
		unit_condition_filter = f" AND {unit_condition_ident} IS NOT NULL AND TRIM({unit_condition_ident}::text) <> ''"
	
	for field in OCCUPANCY_FILTER_FIELDS:
		col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			options[field['key']] = []
			continue
		ident = _quote_ident(col)
		sql = (
			f"SELECT DISTINCT {ident} FROM {OCCUPANCY_DRILL_VIEW} "
			f"WHERE {ident} IS NOT NULL AND TRIM({ident}::text) <> ''{unit_condition_filter} "
			f"ORDER BY {ident} ASC LIMIT 400"
		)
		try:
			with connection.cursor() as cur:
				cur.execute(sql)
				vals = [row[0] for row in cur.fetchall()]
		except Exception as exc:
			print(f"Occupancy drill filter options failed for {field['key']}:", exc)
			vals = []
		options[field['key']] = [str(v).strip() for v in vals if v]
	return options


def _fetch_occupancy_summary(filter_clauses, filter_params, columns):
	"""Calculate summary metrics for occupancy drill-through.
	
	NOTE: Unit Count should reflect TOTAL units in the view, not just those with valid unit_condition.
	Other metrics (units_available, avg_vacant_days, etc.) should use the filtered data.
	"""
	from datetime import datetime
	
	summary = {
		'unit_count': 0,
		'available_as_of': None,
		'units_available': 0,
		'avg_vacant_days': None,
		'vacant_ready_30_days': 0,
	}

	# Find relevant columns
	condition_col = _find_column_by_keywords(columns, ['unit', 'condition', 'status'])
	status_col = _find_column_by_keywords(columns, ['status'])
	turn_time_col = _find_column_by_keywords(columns, ['turn', 'time'])
	days_vacant_col = _find_column_by_keywords(columns, ['days', 'until', 'vacant'])
	date_available_col = _find_column_by_keywords(columns, ['date', 'available', 'unit'])
	
	# Calculate TOTAL unit count excluding the base unit_condition IS NOT NULL filter
	# but keeping user-selected filters (community, investor, etc.)
	# This matches total_units behavior: filtered by user selections, not by data completeness
	try:
		# Strip out the automatic unit_condition IS NOT NULL filter that's added in _prepare_occupancy_drill_query
		# but keep all user-selected filters
		user_filter_clauses = []
		user_filter_params = []
		
		param_idx = 0
		for clause in filter_clauses:
			# Skip the base unit_condition IS NOT NULL filter (no params, just a null check)
			if 'IS NOT NULL' in clause and clause.count('%s') == 0:
				continue
			# Keep other filters (user selections)
			user_filter_clauses.append(clause)
			# Count params needed for this clause
			param_count = clause.count('%s')
			user_filter_params.extend(filter_params[param_idx:param_idx + param_count])
			param_idx += param_count
		
		# Prefer counting distinct site/unit identifier for total units
		cols = _get_occupancy_drill_columns()
		site_id_col = _find_exact_column(cols, 'site_id_property_unit_number') or _find_exact_column(cols, 'OneSiteID-Property-Unit') or _find_column_by_keywords(cols, ['site', 'unit', 'id'])
		if site_id_col:
			count_sql = f"SELECT COUNT(DISTINCT {_quote_ident(site_id_col)}) FROM {OCCUPANCY_DRILL_VIEW}"
		else:
			count_sql = f"SELECT COUNT(DISTINCT unit) FROM {OCCUPANCY_DRILL_VIEW}"
		
		# Apply user filters AND exclude Livcor
		inv_col = _find_column_by_keywords(cols, ['investor'])
		local_clauses = list(user_filter_clauses)
		local_params = list(user_filter_params)
		if inv_col:
			inv_ident = _quote_ident(inv_col)
			local_clauses.append(f"LOWER(TRIM({inv_ident}::text)) NOT LIKE %s")
			local_params.append('%livcor%')
		
		if local_clauses:
			count_sql += ' WHERE ' + ' AND '.join(local_clauses)
		
		with connection.cursor() as cur:
			cur.execute(count_sql, local_params)
			total_units = cur.fetchone()[0] or 0
			summary['unit_count'] = total_units
	except Exception as exc:
		print('Occupancy total unit count query failed:', exc)
		import traceback
		traceback.print_exc()
		summary['unit_count'] = 0
	
	# For other metrics, use the FULL filter (including unit_condition)
	sql_parts = []
	
	# Units Available: COUNT distinct unit identifier WHERE "Leased/Not Leased" = 'Not Leased'
	# Prefer the canonical site/unit identifier to avoid duplicates between similarly named units.
	leased_col = _find_exact_column(columns, 'Leased/Not Leased')
	unit_col = _find_exact_column(columns, 'unit')
	unit_ident_expr = None
	if leased_col:
		site_id_col = (
			_find_exact_column(columns, 'site_id_property_unit_number')
			or _find_exact_column(columns, 'OneSiteID-Property-Unit')
			or _find_column_by_keywords(columns, ['site', 'unit', 'id'])
		)
		if site_id_col:
			unit_ident_expr = _quote_ident(site_id_col)
		elif unit_col:
			unit_ident_expr = _quote_ident(unit_col)

	if leased_col and unit_ident_expr:
		leased_ident = _quote_ident(leased_col)
		sql_parts.append(
			f"COUNT(DISTINCT {unit_ident_expr}) FILTER (WHERE LOWER(TRIM({leased_ident}::text)) = 'not leased') AS units_available"
		)
	elif condition_col:
		# Fallback to old logic if Leased/Not Leased column not found
		ident = _quote_ident(condition_col)
		sql_parts.append(
			f"COUNT(*) FILTER (WHERE {ident}::text ILIKE '%Vacant%' AND {ident}::text ILIKE '%Ready%') AS units_available"
		)
		
	# Average vacant days for units not ready
	if turn_time_col:
		turn_ident = _quote_ident(turn_time_col)
		if condition_col:
			cond_ident = _quote_ident(condition_col)
			sql_parts.append(
				f"AVG({turn_ident}) FILTER (WHERE {cond_ident}::text ILIKE '%Not Ready%') AS avg_vacant_days"
			)
		else:
			sql_parts.append(f"AVG({turn_ident}) AS avg_vacant_days")
	
	# Vacant Units Ready > 30 days
	# Logic: COUNT(*) WHERE "Vacant Status" = 'Ready' AND "days_on_market" > 30
	vacant_status_col = _find_exact_column(columns, 'Vacant Status')
	days_on_market_col = _find_exact_column(columns, 'days_on_market')
	
	if vacant_status_col and days_on_market_col:
		vacant_status_ident = _quote_ident(vacant_status_col)
		days_market_ident = _quote_ident(days_on_market_col)
		sql_parts.append(
			f"COUNT(*) FILTER (WHERE {vacant_status_ident}::text = 'Ready' AND {days_market_ident} > 30) AS vacant_ready_30_days"
		)
	elif condition_col and days_vacant_col:
		# Fallback to old logic
		cond_ident = _quote_ident(condition_col)
		days_ident = _quote_ident(days_vacant_col)
		sql_parts.append(
			f"COUNT(*) FILTER (WHERE {cond_ident}::text ILIKE '%Vacant%' AND {cond_ident}::text ILIKE '%Ready%' AND {days_ident} > 30) AS vacant_ready_30_days"
		)
	
	# Get latest snapshot date
	if date_available_col:
		date_ident = _quote_ident(date_available_col)
		sql_parts.append(f"MAX({date_ident}) AS latest_date")

	# Query other metrics WITH full filters (including unit_condition)
	sql = f"SELECT {', '.join(sql_parts)} FROM {OCCUPANCY_DRILL_VIEW}"
	if filter_clauses:
		sql += ' WHERE ' + ' AND '.join(filter_clauses)

	try:
		with connection.cursor() as cur:
			cur.execute(sql, filter_params)
			row = cur.fetchone()
			col_names = [desc[0] for desc in cur.description]
			data = dict(zip(col_names, row if row else []))
	except Exception as exc:
		print('Occupancy drill summary query failed:', exc)
		# Keep the total_units from earlier query
		data = {}

	# Don't override unit_count from the earlier total query
	units_available = data.get('units_available') or 0
	avg_vacant_days = data.get('avg_vacant_days')
	vacant_ready_30_days = data.get('vacant_ready_30_days') or 0
	latest_date = data.get('latest_date')
	
	summary['units_available'] = units_available
	summary['vacant_ready_30_days'] = vacant_ready_30_days
	
	# Format average vacant days
	if avg_vacant_days is not None:
		try:
			summary['avg_vacant_days'] = int(round(float(avg_vacant_days)))
		except Exception:
			summary['avg_vacant_days'] = None
	
	# Format available as of date
	if latest_date:
		try:
			if isinstance(latest_date, str):
				date_obj = datetime.strptime(latest_date, '%Y-%m-%d').date()
			else:
				date_obj = latest_date
			summary['available_as_of'] = date_obj.strftime('%m/%d/%Y')
		except Exception:
			summary['available_as_of'] = str(latest_date)
	else:
		# Default to today's date
		summary['available_as_of'] = datetime.now().strftime('%m/%d/%Y')
	
	return summary


def _prepare_occupancy_drill_query(request):
	"""Prepare occupancy drill-through query components."""
	columns = _get_occupancy_drill_columns()
	if not columns:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {'unit_count': 0},
				'filter_options': {},
				'filters': {},
				'error': 'The occupancy drill-through view is currently unavailable.',
			},
		}

	select_parts = []
	display_columns = []
	alias_order = []
	alias_to_source = {}
	detail_aliases = {}
	
	for field in OCCUPANCY_TABLE_FIELDS:
		col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			continue
		alias = field['key']
		alias_to_source[alias] = col
		alias_order.append(alias)
		display_columns.append({'key': alias, 'label': field['label']})
		select_parts.append(f"{_quote_ident(col)} AS {alias}")

	site_token_col = _find_column_by_keywords(columns, ['site', 'unit', 'id'])
	if site_token_col and 'site_unit_id' not in alias_order:
		alias = '__detail_site_unit_id'
		detail_aliases['site_unit_id'] = alias
		select_parts.append(f"{_quote_ident(site_token_col)} AS {alias}")

	onesite_token_col = _find_column_by_keywords(columns, ['onesite', 'property'])
	if onesite_token_col and 'onesite_id' not in alias_order:
		alias = '__detail_onesite_id'
		detail_aliases['onesite_id'] = alias
		select_parts.append(f"{_quote_ident(onesite_token_col)} AS {alias}")

	dedup_columns = list(alias_to_source.values())
	filter_options = _fetch_occupancy_filter_options(columns)

	if not select_parts:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {'unit_count': 0},
				'filter_options': filter_options,
				'filters': {},
				'error': 'No recognizable columns were found for the occupancy drill-through dataset.',
			},
		}

	filter_clauses = []
	filter_params = []
	active_filters = {}
	
	for field in OCCUPANCY_FILTER_FIELDS:
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

	unit_condition_col = _find_column_by_keywords(columns, ['unit', 'condition'])
	if unit_condition_col:
		filter_clauses.append(f"{_quote_ident(unit_condition_col)} IS NOT NULL")

	return {
		'error': False,
		'columns': columns,
		'display_columns': display_columns,
		'alias_order': alias_order,
		'alias_to_source': alias_to_source,
		'dedup_columns': dedup_columns,
		'select_parts': select_parts,
		'detail_aliases': detail_aliases,
		'filter_clauses': filter_clauses,
		'filter_params': filter_params,
		'active_filters': active_filters,
		'filter_options': filter_options,
	}


def _build_occupancy_drill_context(request):
	"""Build complete context for occupancy drill-through view."""
	query_info = _prepare_occupancy_drill_query(request)
	if query_info.get('error'):
		return query_info['error_context']

	columns = query_info['columns']
	display_columns = query_info['display_columns']
	alias_order = query_info['alias_order']
	select_parts = query_info['select_parts']
	filter_clauses = query_info['filter_clauses']
	filter_params = query_info['filter_params']
	active_filters = query_info['active_filters']
	filter_options = query_info['filter_options']
	detail_aliases = query_info.get('detail_aliases') or {}

	summary = _fetch_occupancy_summary(filter_clauses, filter_params, columns)
	
	# Calculate FILTERED count for table pagination (includes ALL filters including unit_condition)
	# This is separate from the metric card unit_count which shows overall total
	filtered_count = 0
	try:
		cols = _get_occupancy_drill_columns()
		site_id_col = _find_exact_column(cols, 'site_id_property_unit_number') or _find_exact_column(cols, 'OneSiteID-Property-Unit') or _find_column_by_keywords(cols, ['site', 'unit', 'id'])
		if site_id_col:
			count_sql = f"SELECT COUNT(DISTINCT {_quote_ident(site_id_col)}) FROM {OCCUPANCY_DRILL_VIEW}"
		else:
			count_sql = f"SELECT COUNT(DISTINCT unit) FROM {OCCUPANCY_DRILL_VIEW}"
		
		# Use ALL filters (including unit_condition) for table pagination
		if filter_clauses:
			count_sql += ' WHERE ' + ' AND '.join(filter_clauses)
		
		with connection.cursor() as cur:
			cur.execute(count_sql, filter_params)
			filtered_count = cur.fetchone()[0] or 0
	except Exception as exc:
		print('Occupancy filtered count query failed:', exc)
		filtered_count = 0
	
	page_size = OCCUPANCY_DRILL_PAGE_SIZE
	page_param = request.GET.get('page') if hasattr(request, 'GET') else None
	try:
		requested_page = int(page_param) if page_param else 1
	except Exception:
		requested_page = 1
	if requested_page < 1:
		requested_page = 1
	if filtered_count:
		total_pages = (filtered_count + page_size - 1) // page_size
		page = min(requested_page, total_pages)
	else:
		total_pages = 1
		page = 1
	offset = (page - 1) * page_size if filtered_count else 0

	where_sql = ' WHERE ' + ' AND '.join(filter_clauses) if filter_clauses else ''
	order_alias = 'property_name' if 'property_name' in alias_order else (alias_order[0] if alias_order else None)
	data_sql = f"SELECT {', '.join(select_parts)} FROM {OCCUPANCY_DRILL_VIEW}{where_sql}"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} NULLS LAST"
	data_sql += " LIMIT %s OFFSET %s"
	data_params = list(filter_params) + [page_size, offset]

	rows = []
	result_columns = None
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, data_params)
			result = cur.fetchall()
			description = getattr(cur, 'description', None)
			if description:
				result_columns = [col[0] for col in description]
	except Exception as exc:
		print('Occupancy drill-through data query failed:', exc)
		result = []
		result_columns = None

	def _serialize_detail_param(value):
		if value is None:
			return ''
		if isinstance(value, datetime):
			return value.isoformat()
		if isinstance(value, date):
			return value.isoformat()
		if isinstance(value, Decimal):
			return format(value, 'f')
		return str(value)

	if result_columns is None:
		result_columns = list(alias_order)
		for extra_alias in detail_aliases.values():
			if extra_alias not in result_columns:
				result_columns.append(extra_alias)

	for raw in result:
		full_row = dict(zip(result_columns, raw))
		row_dict = {alias: full_row.get(alias) for alias in alias_order}
		ordered_values = []
		# Inline SVGs for icons (avoid external static dependencies)
		check_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>'
		flag_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 22V6"/><path d="M4 6c4 0 6-2 10-2s6 2 6 2-2 4-6 4-6-2-10-2"/></svg>'
		for alias in alias_order:
			# Special-case: show icons for Not Ready / Past Dates based on mr_day_variance
			if alias == 'not_ready_past_dates':
				mr_val = row_dict.get('mr_day_variance')
				# Treat any non-empty value as presence of MR Day Variance
				if mr_val is not None and str(mr_val).strip() not in ('', 'None'):
					ordered_values.append(mark_safe(flag_svg))
				else:
					ordered_values.append(mark_safe(check_svg))
			else:
				ordered_values.append(_format_occupancy_value(alias, row_dict.get(alias)))
		detail_url = ''
		detail_values = {key: full_row.get(alias) for key, alias in detail_aliases.items()}
		unit_token_candidate = (
			detail_values.get('site_unit_id')
			or detail_values.get('onesite_id')
			or row_dict.get('unit_identifier')
			or row_dict.get('unit')
		)
		if unit_token_candidate:
			try:
				detail_path = reverse('total_unit_detail', kwargs={'unit_token': unit_token_candidate})
			except Exception:
				detail_path = ''
			if detail_path:
				query_pairs = []
				property_name_value = row_dict.get('property_name')
				if property_name_value:
					query_pairs.append(('property', property_name_value))
				return_param = request.get_full_path() if hasattr(request, 'get_full_path') else ''
				if return_param:
					query_pairs.append(('return_url', return_param))
				fallback_map = {
					'fallback_site_id': detail_values.get('site_unit_id'),
					'fallback_onesite_id': detail_values.get('onesite_id'),
					'fallback_unit': row_dict.get('unit') or row_dict.get('unit_identifier') or detail_values.get('onesite_id'),
					'fallback_floor_plan': row_dict.get('floor_plan'),
					'fallback_beds_baths': row_dict.get('beds_baths'),
					'fallback_unit_status': row_dict.get('status') or row_dict.get('leased_not_leased'),
					'fallback_availability_date': row_dict.get('date_unit_available') or row_dict.get('move_in_date'),
					'fallback_amenity_value': row_dict.get('amenity_value'),
					'fallback_market_rent': row_dict.get('market_rent'),
					'fallback_lease_rent': row_dict.get('lease_rent'),
					'fallback_lease_term': row_dict.get('lease_term'),
					'fallback_move_out_reason': row_dict.get('move_out_reason'),
				}
				for key, raw_value in fallback_map.items():
					serialized = _serialize_detail_param(raw_value)
					if serialized:
						query_pairs.append((key, serialized))
				if query_pairs:
					detail_url = f"{detail_path}?{urlencode(query_pairs, doseq=True)}"
				else:
					detail_url = detail_path
		rows.append({'values': ordered_values, 'detail_url': detail_url})

	start_index = offset + 1 if filtered_count and rows else 0
	end_index = offset + len(rows)
	if filtered_count and end_index > filtered_count:
		end_index = filtered_count

	base_query_pairs = []
	if hasattr(request.GET, 'lists'):
		for key, values in request.GET.lists():
			if key == 'page':
				continue
			for val in values:
				if val:
					base_query_pairs.append((key, val))
	
	base_drill_url = reverse('occupancy_drillthrough')

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
		'has_next': bool(filtered_count and page < total_pages),
		'prev_url': _build_page_url(page - 1) if page > 1 else '',
		'next_url': _build_page_url(page + 1) if filtered_count and page < total_pages else '',
		'start_index': start_index,
		'end_index': end_index,
		'total_results': filtered_count,
		'total_count': filtered_count,
	}

	export_pairs = [(k, v) for (k, v) in base_query_pairs if k != 'return_url']
	export_base = reverse('occupancy_drillthrough_export')
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


def _fetch_occupancy_chart_data(filter_clauses, filter_params, columns):
	"""Generate chart data for occupancy drill-through visualizations.
	
	Uses snake_case 'unit_condition' column which contains:
	- Leased
	- Non Revenue
	- On Notice
	- Vacant
	"""
	import json
	
	chart_data = {
		'availability': {'labels': [], 'values': []},
		'vacant_status': {'labels': [], 'values': []}
	}
	
	# Use exact column name to get snake_case 'unit_condition' (not title case 'Unit Condition')
	condition_col = _find_exact_column(columns, 'unit_condition')
	
	if not condition_col:
		# Fallback: try finding by keywords
		condition_col = _find_column_by_keywords(columns, ['unit', 'condition'])
	
	if not condition_col:
		return json.dumps(chart_data)
	
	condition_ident = _quote_ident(condition_col)
	where_sql = ' WHERE ' + ' AND '.join(filter_clauses) if filter_clauses else ''
	
	# Availability by Unit Status - Use actual distinct unit_condition values
	# This will show all real categories from the database
	availability_sql = f"""
		SELECT 
			{condition_ident}::text as condition,
			COUNT(*) as cnt
		FROM {OCCUPANCY_DRILL_VIEW}
		{where_sql}
		GROUP BY {condition_ident}::text
		ORDER BY cnt DESC
	"""
	
	try:
		with connection.cursor() as cur:
			if filter_params:
				cur.execute(availability_sql, filter_params)
			else:
				cur.execute(availability_sql)
			for row in cur.fetchall():
				condition, count = row
				if condition and condition.strip():
					chart_data['availability']['labels'].append(condition.strip())
					chart_data['availability']['values'].append(count)
	except Exception as exc:
		print(f'Availability chart data query failed: {exc}')
	
	# Vacant Unit Make Ready Status Chart
	# Uses type_not_ready_past_dates column which contains:
	# - Various type values (NTV Not Leased, Vacant Leased Ready, etc.)
	# - "Not Ready / Past Date" (when not_ready_past_dates = 1)
	# Only show specific categories matching Power BI dashboard:
	# 1. Vacant Not Leased Ready
	# 2. Not Ready / Past Date
	# 3. Vacant Leased Ready
	# 4. Vacant Not Leased Not Ready
	# 5. Vacant Leased Not Ready
	
	type_col = _find_exact_column(columns, 'type_not_ready_past_dates')
	
	if type_col:
		type_ident = _quote_ident(type_col)
		
		# Specific categories to display (matching Power BI)
		target_categories = [
			'Vacant Not Leased Ready',
			'Not Ready / Past Date',
			'Vacant Leased Ready',
			'Vacant Not Leased Not Ready',
			'Vacant Leased Not Ready'
		]
		
		# Get chart data grouped by type_not_ready_past_dates, filtered to specific categories
		make_ready_sql = f"""
			SELECT 
				{type_ident}::text as category,
				COUNT(*) as cnt
			FROM {OCCUPANCY_DRILL_VIEW}
			{where_sql}
			GROUP BY {type_ident}::text
			HAVING {type_ident}::text IN %s
			ORDER BY cnt DESC
		"""
		
		try:
			with connection.cursor() as cur:
				# Add target categories to params
				params = list(filter_params) if filter_params else []
				params.append(tuple(target_categories))
				
				cur.execute(make_ready_sql, params)
				for row in cur.fetchall():
					category, count = row
					if category and category.strip():
						chart_data['vacant_status']['labels'].append(category.strip())
						chart_data['vacant_status']['values'].append(count)
		except Exception as exc:
			print(f'Make ready status chart data query failed: {exc}')
	
	return json.dumps(chart_data)


@login_required
def occupancy_drillthrough(request):
	"""Main occupancy drill-through view."""
	context = _build_occupancy_drill_context(request)
	context['filter_fields'] = [{'key': field['key'], 'label': field['label']} for field in OCCUPANCY_FILTER_FIELDS]
	context['page_title'] = 'Occupancy Drill-Through'
	context['row_limit'] = OCCUPANCY_DRILL_PAGE_SIZE
	context.setdefault('error', '')
	context.setdefault('export_url', '')
	
	dashboard_return_url = request.GET.get('return_url') or reverse('dashboard')
	context['dashboard_return_url'] = dashboard_return_url
	
	reset_base = reverse('occupancy_drillthrough')
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
	
	# Generate chart data
	query_info = _prepare_occupancy_drill_query(request)
	if not query_info.get('error'):
		columns = query_info['columns']
		filter_clauses = query_info['filter_clauses']
		filter_params = query_info['filter_params']
		context['chart_data'] = _fetch_occupancy_chart_data(filter_clauses, filter_params, columns)
	else:
		context['chart_data'] = '{"availability": {"labels": [], "values": []}, "vacant_status": {"labels": [], "values": []}}'
	
	return render(request, 'dashboard/occupancy_drillthrough.html', context)


@login_required
def move_out_reasons_drillthrough(request):
	"""Move-Out Reasons drill-through page.

	Queries the materialized view `web_ai.move_out_reasons_drill_through` and
	returns context in the standardized drill-through format (table_columns,
	table_rows, pagination, filters, filter_blocks, export_url).
	
	Defaults to showing the latest month of move-out data if no dates are specified.
	"""
	from django.db import connection
	from datetime import datetime, date
	from decimal import Decimal
	from dateutil.relativedelta import relativedelta

	# Get latest available move-out date to default to latest month
	latest_move_out = None
	try:
		with connection.cursor() as cursor:
			cursor.execute('SELECT MAX("Move-Out Date") FROM "web_ai"."move_out_reasons_drill_through" WHERE "Move-Out Date" IS NOT NULL')
			result = cursor.fetchone()
			if result and result[0]:
				latest_move_out = result[0]
	except Exception:
		pass

	# Read filters from request
	community = request.GET.get('community', '')
	regional_vp = request.GET.get('regional_vp', '')
	regional_manager = request.GET.get('regional_manager', '')
	start_date = request.GET.get('start_date', '')
	end_date = request.GET.get('end_date', '')

	# Default to latest month if no dates specified
	if not start_date and not end_date and latest_move_out:
		# Set to first and last day of the latest month
		if isinstance(latest_move_out, str):
			latest_move_out = datetime.strptime(latest_move_out, '%Y-%m-%d').date()
		end_date = latest_move_out.strftime('%Y-%m-%d')
		first_day_of_month = latest_move_out.replace(day=1)
		start_date = first_day_of_month.strftime('%Y-%m-%d')

	filter_clauses = []
	params = []
	if community:
		filter_clauses.append('community = %s')
		params.append(community)
	if regional_vp:
		filter_clauses.append('"Regional VP  | Sr. VP" = %s')
		params.append(regional_vp)
	if regional_manager:
		filter_clauses.append('regional_area_manager = %s')
		params.append(regional_manager)
	# Use Move-Out Date as the date filter column
	if start_date:
		filter_clauses.append('"Move-Out Date" >= %s')
		params.append(start_date)
	if end_date:
		filter_clauses.append('"Move-Out Date" <= %s')
		params.append(end_date)

	# Exclude records without a Move-Out Date — this drill-through only shows move-outs
	filter_clauses.append('"Move-Out Date" IS NOT NULL')

	where_sql = (' WHERE ' + ' AND '.join(filter_clauses)) if filter_clauses else ''

	with connection.cursor() as cursor:
		# distinct options for filters
		try:
			cursor.execute('SELECT DISTINCT community FROM "web_ai"."move_out_reasons_drill_through" WHERE community IS NOT NULL ORDER BY community')
			communities = [row[0] for row in cursor.fetchall()]
		except Exception:
			communities = []
		try:
			cursor.execute('SELECT DISTINCT "Regional VP  | Sr. VP" FROM "web_ai"."move_out_reasons_drill_through" WHERE "Regional VP  | Sr. VP" IS NOT NULL ORDER BY "Regional VP  | Sr. VP"')
			regional_vps = [row[0] for row in cursor.fetchall()]
		except Exception:
			regional_vps = []
		try:
			cursor.execute('SELECT DISTINCT regional_area_manager FROM "web_ai"."move_out_reasons_drill_through" WHERE regional_area_manager IS NOT NULL ORDER BY regional_area_manager')
			regional_managers = [row[0] for row in cursor.fetchall()]
		except Exception:
			regional_managers = []
		investors = []

		# Count for pagination
		try:
			cursor.execute(f'SELECT COUNT(*) FROM "web_ai"."move_out_reasons_drill_through" {where_sql}', params)
			total_records = cursor.fetchone()[0] or 0
		except Exception:
			total_records = 0

		# Calculate metrics for executive summary
		metrics = {
			'total_move_outs': total_records,
			'date_range': f"{start_date} to {end_date}" if start_date and end_date else 'All dates',
			'category_breakdown': [],
		}
		
		# Get category and reason breakdown for expandable table
		try:
			cursor.execute(f'''
				SELECT 
					"Move-Out Category",
					"Move-Out Reason",
					COUNT(*) as cnt
				FROM "web_ai"."move_out_reasons_drill_through" 
				{where_sql}
				GROUP BY "Move-Out Category", "Move-Out Reason"
				ORDER BY "Move-Out Category", cnt DESC
			''', params)
			results = cursor.fetchall()
			
			# Organize by category with nested reasons
			category_dict = {}
			for row in results:
				category = row[0] or 'Unknown'
				reason = row[1] or 'Not specified'
				count = row[2]
				
				if category not in category_dict:
					category_dict[category] = {
						'name': category,
						'count': 0,
						'reasons': []
					}
				
				category_dict[category]['count'] += count
				category_dict[category]['reasons'].append({
					'name': reason,
					'count': count
				})
			
			# Convert to list and sort by total count
			metrics['category_breakdown'] = sorted(
				category_dict.values(),
				key=lambda x: x['count'],
				reverse=True
			)
		except Exception as exc:
			print(f'Move-out category breakdown query failed: {exc}')

		page_size = 100
		try:
			page = int(request.GET.get('page', 1))
		except Exception:
			page = 1
		if page < 1:
			page = 1
		offset = (page - 1) * page_size
		total_pages = (total_records + page_size - 1) // page_size if total_records else 1

		data_sql = f'''
			SELECT property_name, community, "Regional VP  | Sr. VP", regional_area_manager,
				   "Lease ID", unit, "Beds/Baths", "Lease Rent", "Lease Term",
				   "Move-In Date", "Move-Out Date", "Move-Out Category", "Move-Out Reason", "OneSiteID-Property-Unit"
			FROM "web_ai"."move_out_reasons_drill_through"
			{where_sql}
			ORDER BY "Move-Out Date" DESC
			LIMIT %s OFFSET %s
		'''
		try:
			cursor.execute(data_sql, params + [page_size, offset])
			raw_rows = cursor.fetchall()
		except Exception as exc:
			print('Move-out drill-through data query failed:', exc)
			raw_rows = []

	# Format rows
	table_rows = []
	def _format_money_field(v):
		"""Return formatted money string like "$1,234.56" for various input types."""
		if v is None:
			return '-'
		# numeric types
		try:
			if isinstance(v, Decimal):
				num = float(v)
			elif isinstance(v, (int, float)):
				num = float(v)
			else:
				s = str(v).strip()
				# remove common currency formatting
				s = s.replace('$', '').replace(',', '').replace('\u00A0', '')
				# handle parentheses for negative values
				if s.startswith('(') and s.endswith(')'):
					s = '-' + s[1:-1]
				# strip any spaces
				s = s.strip()
				num = float(s)
		except Exception:
			return str(v)
		return f"${num:,.2f}"

	for r in raw_rows:
		# r: property_name, community, Regional VP, regional_area_manager, Lease ID, unit,
		#    Beds/Baths, Lease Rent, Lease Term, Move-In Date, Move-Out Date, Move-Out Category, Move-Out Reason, OneSiteID
		formatted = [
			r[0] or '-',
			r[1] or '-',
			r[2] or '-',
			r[3] or '-',
			r[4] or '-',
			r[5] or '-',
			r[6] or '-',
			_format_money_field(r[7]),
			(str(r[8]) if r[8] is not None else '-'),
			(r[9].strftime('%Y-%m-%d') if hasattr(r[9], 'strftime') else (str(r[9]) if r[9] else '-')),
			(r[10].strftime('%Y-%m-%d') if hasattr(r[10], 'strftime') else (str(r[10]) if r[10] else '-')),
			r[11] or '-',
			r[12] or '-',
			r[13] or '-',
		]
		table_rows.append(formatted)

	table_columns = [
		{'label': 'Property Name'},
		{'label': 'Community'},
		{'label': 'Regional VP'},
		{'label': 'Regional Manager'},
		{'label': 'Lease ID'},
		{'label': 'Unit'},
		{'label': 'Beds/Baths'},
		{'label': 'Lease Rent'},
		{'label': 'Lease Term'},
		{'label': 'Move-In Date'},
		{'label': 'Move-Out Date'},
		{'label': 'Move-Out Category'},
		{'label': 'Move-Out Reason'},
		{'label': 'OneSiteID'},
	]

	# build pagination urls
	base_query_pairs = []
	for key in ['community','regional_vp','regional_manager','start_date','end_date','return_url']:
		v = request.GET.get(key)
		if v:
			base_query_pairs.append((key, v))

	from django.urls import reverse
	from urllib.parse import urlencode
	base_drill_url = reverse('move_out_reasons_drillthrough')
	def _build_page_url(tp):
		pairs = list(base_query_pairs)
		if tp > 1:
			pairs.append(('page', tp))
		q = urlencode(pairs, doseq=True)
		return f"{base_drill_url}?{q}" if q else base_drill_url

	start_index = offset + 1 if total_records and table_rows else 0
	end_index = offset + len(table_rows)
	if total_records and end_index > total_records:
		end_index = total_records

	pagination = {
		'page': page,
		'page_size': page_size,
		'total_pages': total_pages,
		'has_prev': page > 1,
		'has_next': bool(total_records and page < total_pages),
		'prev_url': _build_page_url(page - 1) if page > 1 else '',
		'next_url': _build_page_url(page + 1) if total_records and page < total_pages else '',
		'start_index': start_index,
		'end_index': end_index,
		'total_count': total_records,
	}

	export_pairs = [(k, v) for (k, v) in base_query_pairs if k != 'return_url']
	# Include current page in export URL so export can match the displayed table page
	try:
		current_page = int(request.GET.get('page', 1))
		if current_page > 1:
			export_pairs.append(('page', current_page))
	except Exception:
		pass
	export_base = reverse('move_out_reasons_drillthrough_export') if 'move_out_reasons_drillthrough_export' in globals() else base_drill_url
	export_query = urlencode(export_pairs, doseq=True)
	export_url = f"{export_base}?{export_query}" if export_query else export_base

	context = {
		'page_title': 'Move-Out Reasons Drill-Through',
		'table_columns': table_columns,
		'table_rows': table_rows,
		'pagination': pagination,
		'metrics': metrics,
		'filters': {
			'community': community,
			'regional_vp': regional_vp,
			'regional_manager': regional_manager,
			'start_date': start_date,
			'end_date': end_date,
		},
		'filter_blocks': [
			{'key': 'community', 'label': 'Community', 'options': communities, 'selected': community},
			{'key': 'regional_vp', 'label': 'Regional VP', 'options': regional_vps, 'selected': regional_vp},
			{'key': 'regional_manager', 'label': 'Regional Manager', 'options': regional_managers, 'selected': regional_manager},
		],
		'filter_options': {'community': communities, 'regional_vp': regional_vps, 'regional_manager': regional_managers},
		'export_url': export_url,
		'dashboard_return_url': request.GET.get('return_url') or reverse('dashboard'),
		'drill_reset_url': base_drill_url,
	}

	return render(request, 'dashboard/move_out_reasons_drillthrough.html', context)


@login_required
def occupancy_drillthrough_export(request):
	"""CSV export for occupancy drill-through."""
	query_info = _prepare_occupancy_drill_query(request)
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

	from django.utils import timezone
	# Build SQL and fetch all filtered rows (no pagination)
	# NOTE: the UI summary intentionally counts total units excluding the
	# base "unit_condition IS NOT NULL" clause so the header shows the
	# full dataset size. To make the CSV match the header (and the
	# dashboard expectation), reconstruct the WHERE clause for export by
	# skipping the base unit_condition filter if present.
	export_clauses = []
	export_params = []
	param_idx = 0
	for clause in filter_clauses:
		# Skip the base unit_condition filter which was added by
		# _prepare_occupancy_drill_query to restrict rows for the table view.
		if 'unit_condition' in clause.replace('"', '').lower():
			# consume param placeholders for this clause
			param_count = clause.count('%s')
			param_idx += param_count
			continue
		# keep this clause and its params
		export_clauses.append(clause)
		param_count = clause.count('%s')
		if param_count:
			export_params.extend(filter_params[param_idx:param_idx + param_count])
		param_idx += param_count

	where_sql = ' WHERE ' + ' AND '.join(export_clauses) if export_clauses else ''
	order_alias = 'property_name' if 'property_name' in alias_order else (alias_order[0] if alias_order else None)
	data_sql = f"SELECT {', '.join(select_parts)} FROM {OCCUPANCY_DRILL_VIEW}{where_sql}"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} NULLS LAST"

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, export_params)
			rows = cur.fetchall()
	except Exception as exc:
		print('Occupancy drill-through export failed:', exc)
		return HttpResponse('Failed to export data.', status=500)

	timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
	filename = f"occupancy_drillthrough_{timestamp}.csv"
	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = f'attachment; filename="{filename}"'

	writer = csv.writer(response)
	writer.writerow([col['label'] for col in display_columns])
	for raw in rows:
		row_dict = dict(zip(alias_order, raw))
		writer.writerow([_format_occupancy_value(alias, row_dict.get(alias)) for alias in alias_order])

	return response


@login_required
def move_out_reasons_drillthrough_export(request):
	"""CSV export for Move-Out Reasons drill-through."""
	from django.db import connection
	import csv
	from django.utils import timezone

	# Read filters from request (same as the drill view)
	community = request.GET.get('community', '')
	regional_vp = request.GET.get('regional_vp', '')
	regional_manager = request.GET.get('regional_manager', '')
	start_date = request.GET.get('start_date', '')
	end_date = request.GET.get('end_date', '')

	filter_clauses = []
	params = []
	if community:
		filter_clauses.append('community = %s')
		params.append(community)
	if regional_vp:
		filter_clauses.append('"Regional VP  | Sr. VP" = %s')
		params.append(regional_vp)
	if regional_manager:
		filter_clauses.append('regional_area_manager = %s')
		params.append(regional_manager)
	if start_date:
		filter_clauses.append('"Move-Out Date" >= %s')
		params.append(start_date)
	if end_date:
		filter_clauses.append('"Move-Out Date" <= %s')
		params.append(end_date)

	# Exclude records without a Move-Out Date — this drill-through only shows move-outs
	filter_clauses.append('"Move-Out Date" IS NOT NULL')

	where_sql = (' WHERE ' + ' AND '.join(filter_clauses)) if filter_clauses else ''

	data_sql = f'''
		SELECT property_name, community, "Regional VP  | Sr. VP", regional_area_manager,
			   "Lease ID", unit, "Beds/Baths", "Lease Rent", "Lease Term",
			   "Move-In Date", "Move-Out Date", "Move-Out Category", "Move-Out Reason", "OneSiteID-Property-Unit"
		FROM "web_ai"."move_out_reasons_drill_through"
		{where_sql}
		ORDER BY "Move-Out Date" DESC
	'''

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, params)
			rows = cur.fetchall()
	except Exception as exc:
		print('Move-out drill-through export failed:', exc)
		return HttpResponse('Failed to export data.', status=500)

	# Header labels (match table_columns in the drill view)
	header = [
		'Property Name', 'Community', 'Regional VP', 'Regional Manager', 'Lease ID', 'Unit',
		'Beds/Baths', 'Lease Rent', 'Lease Term', 'Move-In Date', 'Move-Out Date',
		'Move-Out Category', 'Move-Out Reason', 'OneSiteID',
	]

	# reuse formatting logic from the drill view
	from decimal import Decimal

	def _format_money_field(v):
		if v is None:
			return ''
		try:
			if isinstance(v, Decimal):
				num = float(v)
			elif isinstance(v, (int, float)):
				num = float(v)
			else:
				s = str(v).strip()
				s = s.replace('$', '').replace(',', '').replace('\u00A0', '')
				if s.startswith('(') and s.endswith(')'):
					s = '-' + s[1:-1]
				s = s.strip()
				num = float(s)
		except Exception:
			return str(v)
		return f"${num:,.2f}"

	timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
	filename = f"move_out_reasons_drillthrough_{timestamp}.csv"
	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = f'attachment; filename="{filename}"'

	writer = csv.writer(response)
	writer.writerow(header)
	for r in rows:
		row = [
			r[0] or '',
			r[1] or '',
			r[2] or '',
			r[3] or '',
			r[4] or '',
			r[5] or '',
			r[6] or '',
			_format_money_field(r[7]),
			(str(r[8]) if r[8] is not None else ''),
			(r[9].strftime('%Y-%m-%d') if hasattr(r[9], 'strftime') else (str(r[9]) if r[9] else '')),
			(r[10].strftime('%Y-%m-%d') if hasattr(r[10], 'strftime') else (str(r[10]) if r[10] else '')),
			r[11] or '',
			r[12] or '',
			r[13] or '',
		]
		writer.writerow(row)

	return response



# ============================================================================
# EXPOSURE DRILL-THROUGH VIEWS
# ============================================================================

EXPOSURE_DRILL_VIEW = 'web_ai.total_unit_occupancy_drill_through'
EXPOSURE_DRILL_PAGE_SIZE = 500

EXPOSURE_FILTER_FIELDS = [
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'keywords': ['regional', 'vp']},
	{'key': 'regional_manager', 'label': 'Regional Manager', 'keywords': ['regional', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
	{'key': 'floor_plan', 'label': 'Floor Plan', 'keywords': ['floor', 'plan']},
]

EXPOSURE_TABLE_FIELDS = [
	{'key': 'property_name', 'label': 'Property Name', 'keywords': ['property', 'name']},
	{'key': 'unit_condition', 'label': 'Unit Condition', 'exact': 'Unit Condition'},
	{'key': 'unit', 'label': 'Unit', 'keywords': ['unit'], 'exact': 'unit'},
	{'key': 'floor_plan', 'label': 'Floor Plan', 'exact': 'Floor Plan'},
	{'key': 'beds_baths', 'label': 'Beds / Baths', 'exact': 'Beds/Baths'},
	{'key': 'floor_level', 'label': 'Floor Level', 'keywords': ['floor', 'level']},
	{'key': 'amenity_value', 'label': 'Amenity Value', 'exact': 'Amenity Value'},
	{'key': 'move_out', 'label': 'Move Out', 'keywords': ['move', 'out', 'date']},
	{'key': 'date_unit_available', 'label': 'Date Unit Available', 'keywords': ['make', 'ready', 'date']},
	{'key': 'days_until_vacant', 'label': 'Days Until Available', 'keywords': ['days', 'until', 'vacant']},
	{'key': 'turn_time', 'label': 'Turn Time', 'keywords': ['turn', 'time']},
	{'key': 'status', 'label': 'Status', 'keywords': ['type'], 'exact': 'type'},
	{'key': 'not_ready_past_dates', 'label': 'Not Ready/ Past Dates', 'keywords': ['not', 'ready', 'past']},
	{'key': 'mr_day_variance', 'label': 'MR Day Variance', 'keywords': ['mr', 'day', 'variance']},
	{'key': 'leased_not_leased', 'label': 'Leased/Not Leased', 'exact': 'Leased/Not Leased'},
	{'key': 'days_on_market', 'label': 'Days On Market', 'keywords': ['days', 'market']},
	{'key': 'market_rent', 'label': 'Market Rent', 'exact': 'Market Rent'},
	{'key': 'lease_rent', 'label': 'Lease Rent', 'keywords': ['lease', 'rent']},
	{'key': 'lease_term', 'label': 'Lease Term', 'exact': 'Lease Term'},
	{'key': '12_month_price', 'label': '12 Month Price', 'keywords': ['monthly', 'effective', 'rent']},
	{'key': 'best_term', 'label': 'Best Term', 'keywords': ['best', 'price', 'term']},
	{'key': 'best_price', 'label': 'Best Price', 'keywords': ['best', 'price', 'monthly']},
	{'key': 'forecasted_trade_out', 'label': 'Forecasted Trade out', 'keywords': ['forecasted', 'trade']},
	{'key': 'move_out_reason', 'label': 'Move out Reason', 'keywords': ['move', 'out', 'reason']},
	{'key': 'onesite_id', 'label': 'OneSiteID | Property # | Unit #', 'keywords': ['site', 'id', 'property', 'unit', 'number']},
]

EXPOSURE_CURRENCY_FIELDS = {'amenity_value', 'market_rent', 'lease_rent', '12_month_price', 'best_price', 'forecasted_trade_out'}
EXPOSURE_DATE_FIELDS = {'move_out', 'date_unit_available'}
EXPOSURE_PERCENTAGE_FIELDS = set()


def _get_exposure_drill_columns():
	"""Get all columns from the exposure drill-through materialized view."""
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
		print('Exposure drill-through column introspection failed:', exc)

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
		print('Exposure drill-through pg_attribute introspection failed:', exc)
	return columns


def _format_exposure_value(alias, value):
	"""Format exposure drill-through values for display."""
	if value is None:
		return '--'
	if isinstance(value, (datetime, date)):
		return value.strftime('%b %d, %Y')
	if isinstance(value, Decimal):
		value = float(value)
	if alias in EXPOSURE_CURRENCY_FIELDS:
		try:
			return f"${float(value):,.2f}"
		except Exception:
			# Avoid double-dollar when DB already returns formatted string
			s = str(value).strip()
			if '$' in s:
				return s
			try:
				s2 = s.replace(',', '').replace('\u00A0', '')
				if s2.startswith('(') and s2.endswith(')'):
					s2 = '-' + s2[1:-1]
				num = float(s2)
				return f"${num:,.2f}"
			except Exception:
				return s
	if alias in EXPOSURE_PERCENTAGE_FIELDS:
		try:
			return f"{float(value):.2f}%"
		except Exception:
			return f"{value}%"
	if alias in EXPOSURE_DATE_FIELDS:
		try:
			parsed = datetime.strptime(str(value), '%Y-%m-%d').date()
			return parsed.strftime('%b %d, %Y')
		except Exception:
			return str(value)
	text = str(value).strip()
	return text or '--'


def _fetch_exposure_filter_options(columns):
	"""Fetch distinct filter options for exposure drill-through filters."""
	options = {}
	
	for field in EXPOSURE_FILTER_FIELDS:
		col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			options[field['key']] = []
			continue
		ident = _quote_ident(col)
		sql = (
			f"SELECT DISTINCT {ident} FROM {EXPOSURE_DRILL_VIEW} "
			f"WHERE {ident} IS NOT NULL AND TRIM({ident}::text) <> '' "
			f"ORDER BY {ident} ASC LIMIT 400"
		)
		try:
			with connection.cursor() as cur:
				cur.execute(sql)
				vals = [row[0] for row in cur.fetchall()]
		except Exception as exc:
			print(f"Exposure drill filter options failed for {field['key']}:", exc)
			vals = []
		# Exclude Livcor from investor options (business rule: Livcor properties should not be shown)
		if field['key'] == 'investor':
			vals = [v for v in vals if str(v).upper() not in ['BLACKSTONE/LIVCOR', 'LIVCOR']]
		options[field['key']] = [str(v).strip() for v in vals if v]
	return options


def _fetch_exposure_summary(filter_clauses, filter_params, columns):
	"""Calculate summary metrics for exposure drill-through.
	
	Metrics:
	- Unit Count: Total units in view
	- Units Not Leased: COUNT(DISTINCT unit) WHERE "Leased/Not Leased" = 'Not Leased'
	- Exposure 8 Weeks: AVG(exposure_8_weeks) as percentage
	- Forecasted Occupancy 8 Weeks: 100% - Exposure 8 Weeks
	- Current Occupancy: (Total Units - Units Not Leased) / Total Units * 100
	"""
	from datetime import datetime
	
	summary = {
		'unit_count': 0,
		'exposure_as_of': None,
		'units_not_leased': 0,
		'exposure_8_weeks': None,
		'forecasted_occupancy_8_weeks': None,
		'current_occupancy': None,
		'exposure_units': None,
	}

	# Find relevant columns
	leased_col = _find_exact_column(columns, 'Leased/Not Leased')
	unit_col = _find_exact_column(columns, 'unit')
	exposure_col = _find_column_by_keywords(columns, ['exposure', '8', 'weeks'])
	date_available_col = _find_column_by_keywords(columns, ['date', 'available', 'unit'])
	
	# Calculate TOTAL unit count (all units in view)
	try:
		count_sql = f"SELECT COUNT(*) FROM {EXPOSURE_DRILL_VIEW}"
		# Exclude rows with null/empty property_name from the total unit count
		if filter_clauses:
			count_sql += ' WHERE ' + ' AND '.join(filter_clauses)
			count_sql += " AND property_name IS NOT NULL AND TRIM(property_name::text) <> ''"
		else:
			count_sql += " WHERE property_name IS NOT NULL AND TRIM(property_name::text) <> ''"
		
		with connection.cursor() as cur:
			cur.execute(count_sql, filter_params)
			total_units = cur.fetchone()[0] or 0
			summary['unit_count'] = total_units
	except Exception as exc:
		print('Exposure total unit count query failed:', exc)
		import traceback
		traceback.print_exc()
		summary['unit_count'] = 0

	# Prepare for DAX-style exposure calculation (vacant/NTV within 8 weeks)
	exposure_units = None
	exposure_ratio = None
	unit_type_candidates = [
		_find_exact_column(columns, 'unit_type'),
		_find_exact_column(columns, 'type'),
		_find_exact_column(columns, 'Unit Type'),
		_find_exact_column(columns, 'Vacant Status'),
		_find_column_by_keywords(columns, ['vacant', 'status']),
		_find_column_by_keywords(columns, ['unit', 'type'])
	]
	move_out_candidates = [
		_find_exact_column(columns, 'move_out_date'),
		_find_exact_column(columns, 'move_out'),
		_find_column_by_keywords(columns, ['move', 'out'])
	]
	unit_type_col = next((col for col in unit_type_candidates if col), None)
	move_out_col = next((col for col in move_out_candidates if col), None)
	if total_units and unit_type_col and move_out_col:
		metric_columns = {
			'unit_type': unit_type_col,
			'move_out_date': move_out_col,
		}
		exposure_units, exposure_ratio = _compute_exposure_8_weeks(filter_clauses, filter_params, metric_columns, total_units)
		if exposure_units is not None:
			summary['exposure_units'] = exposure_units
	
	# Build metrics query
	sql_parts = []
	
	# Units Not Leased: COUNT(DISTINCT unit) WHERE "Leased/Not Leased" = 'Not Leased'
	# Prefer counting non-null `Unit Condition` values (Power BI: COUNT(fact_availability_history[Unit Condition])
	# filtered by [Leased/Not Leased] = 'Not Leased'. Fall back to counting distinct `unit` if Unit Condition not present.
	unit_condition_col = _find_exact_column(columns, 'Unit Condition') or _find_column_by_keywords(columns, ['unit', 'condition'])
	if leased_col and unit_condition_col:
		leased_ident = _quote_ident(leased_col)
		uc_ident = _quote_ident(unit_condition_col)
		# Count the Unit Condition values (no DISTINCT) where Leased/Not Leased = 'Not Leased'
		sql_parts.append(
			f"COUNT({uc_ident}) FILTER (WHERE {leased_ident}::text = 'Not Leased') AS units_not_leased"
		)
	elif leased_col and unit_col:
		leased_ident = _quote_ident(leased_col)
		unit_ident = _quote_ident(unit_col)
		# Fallback: previous behavior (count distinct unit identifiers)
		sql_parts.append(
			f"COUNT(DISTINCT {unit_ident}) FILTER (WHERE {leased_ident}::text = 'Not Leased') AS units_not_leased"
		)
	
	# Average Exposure 8 Weeks
	if exposure_col:
		exposure_ident = _quote_ident(exposure_col)
		sql_parts.append(f"AVG({exposure_ident}) AS avg_exposure_8_weeks")
	
	# Get latest snapshot date
	if date_available_col:
		date_ident = _quote_ident(date_available_col)
		sql_parts.append(f"MAX({date_ident}) AS latest_date")

	# Query metrics
	if sql_parts:
		sql = f"SELECT {', '.join(sql_parts)} FROM {EXPOSURE_DRILL_VIEW}"
		if filter_clauses:
			sql += ' WHERE ' + ' AND '.join(filter_clauses)

		try:
			with connection.cursor() as cur:
				cur.execute(sql, filter_params)
				row = cur.fetchone()
				col_names = [desc[0] for desc in cur.description]
				data = dict(zip(col_names, row if row else []))
		except Exception as exc:
			print('Exposure drill summary query failed:', exc)
			data = {}
	else:
		data = {}

	units_not_leased = data.get('units_not_leased') or 0
	avg_exposure_8_weeks = data.get('avg_exposure_8_weeks')
	latest_date = data.get('latest_date')
	
	summary['units_not_leased'] = units_not_leased
	
	# Calculate Exposure 8 Weeks percentage using DAX-style ratio if available
	if exposure_ratio is not None:
		try:
			exposure_pct = max(min(exposure_ratio * 100.0, 100.0), 0.0)
			summary['exposure_8_weeks'] = f"{exposure_pct:.2f}%"
			forecasted_ratio = 1.0 - exposure_ratio
			forecasted_pct = max(min(forecasted_ratio * 100.0, 100.0), 0.0)
			summary['forecasted_occupancy_8_weeks'] = f"{forecasted_pct:.2f}%"
		except Exception:
			summary['exposure_8_weeks'] = 'N/A'
			summary['forecasted_occupancy_8_weeks'] = 'N/A'
	else:
		# Fallback to average column if ratio not available
		if avg_exposure_8_weeks is not None:
			try:
				exposure_pct = float(avg_exposure_8_weeks)
				summary['exposure_8_weeks'] = f"{exposure_pct:.2f}%"
				forecasted_pct = 100.0 - exposure_pct
				summary['forecasted_occupancy_8_weeks'] = f"{forecasted_pct:.2f}%"
			except Exception:
				summary['exposure_8_weeks'] = '--'
				summary['forecasted_occupancy_8_weeks'] = '--'
		else:
			if total_units == 0:
				summary['exposure_8_weeks'] = 'N/A'
				summary['forecasted_occupancy_8_weeks'] = 'N/A'
			else:
				summary['exposure_8_weeks'] = '--'
				summary['forecasted_occupancy_8_weeks'] = '--'
	
	# Calculate Current Occupancy
	# Power BI logic: count non-null Status (type column) rows excluding "NTV Leased" and "NTV Not Leased"
	# These are nonoccupiable units. Occupancy = (total_units - nonoccupiable_units) / total_units * 100
	if total_units > 0:
		try:
			# Find the status/type column
			status_col = unit_type_col  # Already resolved earlier as 'type' column
			if status_col:
				status_ident = _quote_ident(status_col)
				# Count rows where status IS NOT NULL and status NOT IN ('NTV Leased', 'NTV Not Leased')
				nonoccupiable_sql = f"""
					SELECT COUNT(*) 
					FROM {EXPOSURE_DRILL_VIEW}
					WHERE {status_ident} IS NOT NULL
					  AND {status_ident}::text NOT IN ('NTV Leased', 'NTV Not Leased')
				"""
				if filter_clauses:
					nonoccupiable_sql += ' AND ' + ' AND '.join(filter_clauses)
				
				with connection.cursor() as cur:
					cur.execute(nonoccupiable_sql, filter_params)
					nonoccupiable_units = cur.fetchone()[0] or 0
				
				# Calculate occupancy
				current_occupancy_pct = ((total_units - nonoccupiable_units) / total_units) * 100.0
				summary['current_occupancy'] = f"{current_occupancy_pct:.2f}%"
			else:
				# Fallback: use previous logic (total_units - units_not_leased)
				occupied_units = total_units - units_not_leased
				current_occupancy_pct = (occupied_units / total_units) * 100.0
				summary['current_occupancy'] = f"{current_occupancy_pct:.2f}%"
		except Exception as exc:
			print('Current Occupancy calculation failed:', exc)
			summary['current_occupancy'] = '--'
	else:
		summary['current_occupancy'] = '--'
	
	# Format exposure as of date
	if latest_date:
		try:
			if isinstance(latest_date, str):
				date_obj = datetime.strptime(latest_date, '%Y-%m-%d').date()
			else:
				date_obj = latest_date
			summary['exposure_as_of'] = date_obj.strftime('%m/%d/%Y')
		except Exception:
			summary['exposure_as_of'] = str(latest_date)
	else:
		# Default to today's date
		summary['exposure_as_of'] = datetime.now().strftime('%m/%d/%Y')
	
	return summary


def _prepare_exposure_drill_query(request):
	"""Prepare exposure drill-through query components."""
	columns = _get_exposure_drill_columns()
	if not columns:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {'unit_count': 0},
				'filter_options': {},
				'filters': {},
				'error': 'The exposure drill-through view is currently unavailable.',
			},
		}

	select_parts = []
	display_columns = []
	alias_order = []
	alias_to_source = {}
	
	for field in EXPOSURE_TABLE_FIELDS:
		# Try exact match first if specified
		col = None
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		# Fall back to keyword search
		if not col and 'keywords' in field:
			col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			continue
		alias = field['key']
		alias_to_source[alias] = col
		alias_order.append(alias)
		display_columns.append({'key': alias, 'label': field['label']})
		# Quote alias if it starts with a number
		quoted_alias = _quote_ident(alias) if alias[0].isdigit() else alias
		select_parts.append(f"{_quote_ident(col)} AS {quoted_alias}")

	filter_options = _fetch_exposure_filter_options(columns)

	if not select_parts:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {'unit_count': 0},
				'filter_options': filter_options,
				'filters': {},
				'error': 'No recognizable columns were found for the exposure drill-through dataset.',
			},
		}

	filter_clauses = []
	filter_params = []
	active_filters = {}
	
	# Base filters to apply to ALL queries (including total unit count)
	base_filter_clauses = []
	base_filter_params = []
	
	# 1. Exclude Livcor properties (business rule)
	investor_col = _find_column_by_keywords(columns, ['investor'])
	if investor_col:
		investor_ident = _quote_ident(investor_col)
		base_filter_clauses.append(f"({investor_ident} IS NULL OR ({investor_ident}::text NOT ILIKE %s AND {investor_ident}::text NOT ILIKE %s))")
		base_filter_params.extend(['%BLACKSTONE/LIVCOR%', '%LIVCOR%'])
	
	# Copy base filters to main filter lists
	filter_clauses.extend(base_filter_clauses)
	filter_params.extend(base_filter_params)
	
	# 2. Add unit_condition filter for TABLE DATA ONLY (not for unit count)
	# This filter will be added separately when fetching table rows
	unit_condition_col = _find_exact_column(columns, 'Unit Condition')
	if not unit_condition_col:
		unit_condition_col = _find_column_by_keywords(columns, ['unit', 'condition'])
	
	for field in EXPOSURE_FILTER_FIELDS:
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
		'unit_condition_col': unit_condition_col,  # For table-level filtering
	}


def _build_exposure_drill_context(request):
	"""Build complete context for exposure drill-through view."""
	query_info = _prepare_exposure_drill_query(request)
	if query_info.get('error'):
		return query_info['error_context']

	columns = query_info['columns']
	display_columns = query_info['display_columns']
	alias_order = query_info['alias_order']
	select_parts = query_info['select_parts']
	filter_clauses = query_info['filter_clauses']
	filter_params = query_info['filter_params']
	active_filters = query_info['active_filters']
	filter_options = query_info['filter_options']
	unit_condition_col = query_info.get('unit_condition_col')

	summary = _fetch_exposure_summary(filter_clauses, filter_params, columns)
	total_units = summary.get('unit_count') or 0
	
	page_size = EXPOSURE_DRILL_PAGE_SIZE
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

	# For TABLE DATA: add unit_condition NOT NULL filter
	table_filter_clauses = list(filter_clauses)
	table_filter_params = list(filter_params)
	if unit_condition_col:
		unit_condition_ident = _quote_ident(unit_condition_col)
		table_filter_clauses.append(f"{unit_condition_ident} IS NOT NULL AND TRIM({unit_condition_ident}::text) <> ''")

	where_sql = ' WHERE ' + ' AND '.join(table_filter_clauses) if table_filter_clauses else ''
	order_alias = 'property_name' if 'property_name' in alias_order else (alias_order[0] if alias_order else None)
	data_sql = f"SELECT {', '.join(select_parts)} FROM {EXPOSURE_DRILL_VIEW}{where_sql}"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} NULLS LAST"
	data_sql += " LIMIT %s OFFSET %s"
	data_params = table_filter_params + [page_size, offset]

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, data_params)
			result = cur.fetchall()
	except Exception as exc:
		print('Exposure drill-through data query failed:', exc)
		result = []
	
	for raw in result:
		row_dict = dict(zip(alias_order, raw))
		ordered_values = [_format_exposure_value(alias, row_dict.get(alias)) for alias in alias_order]
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
	
	base_drill_url = reverse('exposure_drillthrough')

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
	export_base = reverse('exposure_drillthrough_export')
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


@login_required
def exposure_drillthrough(request):
	"""Main exposure drill-through view."""
	context = _build_exposure_drill_context(request)
	context['filter_fields'] = [{'key': field['key'], 'label': field['label']} for field in EXPOSURE_FILTER_FIELDS]
	context['page_title'] = 'Exposure Drill-Through'
	context['row_limit'] = EXPOSURE_DRILL_PAGE_SIZE
	context.setdefault('error', '')
	context.setdefault('export_url', '')
	
	dashboard_return_url = request.GET.get('return_url') or reverse('dashboard')
	context['dashboard_return_url'] = dashboard_return_url
	
	reset_base = reverse('exposure_drillthrough')
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
	
	return render(request, 'dashboard/exposure_drillthrough.html', context)


@login_required
def exposure_drillthrough_export(request):
	"""CSV export for exposure drill-through."""
	query_info = _prepare_exposure_drill_query(request)
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
	data_sql = f"SELECT {', '.join(select_parts)} FROM {EXPOSURE_DRILL_VIEW}{where_sql}"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} NULLS LAST"

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, list(filter_params))
			rows = cur.fetchall()
	except Exception as exc:
		print('Exposure drill-through export failed:', exc)
		return HttpResponse('Failed to export data.', status=500)

	timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
	filename = f"exposure_drillthrough_{timestamp}.csv"
	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = f'attachment; filename="{filename}"'

	writer = csv.writer(response)
	writer.writerow([col['label'] for col in display_columns])
	for raw in rows:
		row_dict = dict(zip(alias_order, raw))
		writer.writerow([_format_exposure_value(alias, row_dict.get(alias)) for alias in alias_order])

	return response


# ============================================================================
# DELINQUENCY DRILL-THROUGH VIEWS
# ============================================================================

DELINQUENCY_DRILL_VIEW = 'web_ai.delinquency_drill_through'
DELINQUENCY_DRILL_PAGE_SIZE = 500

DELINQUENCY_FILTER_FIELDS = [
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'keywords': ['regional', 'vp']},
	{'key': 'regional_manager', 'label': 'Regional Manager', 'keywords': ['regional', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
	# Allow selecting a fiscal month-year (exact column name on the MV)
	{'key': 'fiscal_as_of_month_year', 'label': 'Fiscal As Of Month Year', 'exact': 'Fiscal As Of month Year'},
]

DELINQUENCY_TABLE_FIELDS = [
	{'key': 'property_name', 'label': 'Property Name', 'keywords': ['property', 'name']},
	{'key': 'unit_number_name', 'label': 'Unit Number/Name', 'exact': 'Unit_Number/Name'},
	{'key': 'code_description', 'label': 'Code Description', 'keywords': ['code', 'description']},
	{'key': 'delinquency_status', 'label': 'Delinquency Status', 'keywords': ['delinquency', 'status']},
	{'key': 'total_delinquent', 'label': 'Total Delinquent', 'keywords': ['total', 'delinquent']},
	{'key': 'total_prepaid', 'label': 'Total Prepaid', 'keywords': ['total', 'prepaid']},
	{'key': 'days_0_30', 'label': '0-30 Days', 'exact': '0-30 Days'},
	{'key': 'days_30_60', 'label': '30-60 Days', 'exact': '30-60 Days'},
	{'key': 'days_60_90', 'label': '60-90 Days', 'exact': '60-90 Days'},
	{'key': 'days_90_plus', 'label': '90+ Days', 'exact': '90 plus days'},
	{'key': 'prorate_credits', 'label': 'Prorate Credits', 'keywords': ['prorate', 'credits']},
	{'key': 'deposits_held', 'label': 'Deposits Held', 'keywords': ['deposits', 'held']},
	{'key': 'outstanding_deposit', 'label': 'Outstanding Deposit', 'keywords': ['outstanding', 'deposit']},
	{'key': 'is_employee_lease_status', 'label': 'Is Employee Lease', 'keywords': ['employee', 'lease', 'status']},
	{'key': 'late_nsf_value', 'label': 'Late/NSF', 'keywords': ['late', 'nsf', 'value']},
	{'key': 'is_under_eviction', 'label': 'Is Under Eviction', 'keywords': ['eviction']},
	{'key': 'notice_date', 'label': 'Notice Date', 'keywords': ['notice', 'date']},
	{'key': 'fiscal_as_of_month_year', 'label': 'Fiscal As Of Month Year', 'exact': 'Fiscal As Of month Year'},
	{'key': 'unit_number', 'label': 'Unit Number', 'keywords': ['unit', 'number']},
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'exact': 'Regional VP  | Sr. VP'},
	{'key': 'regional_area_manager', 'label': 'Regional Area Manager', 'keywords': ['regional', 'area', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
]

DELINQUENCY_CURRENCY_FIELDS = {
	'total_delinquent', 'total_prepaid', 'days_0_30', 'days_30_60', 'days_60_90', 'days_90_plus',
	'prorate_credits', 'deposits_held', 'outstanding_deposit'
}
DELINQUENCY_DATE_FIELDS = {'notice_date', 'fiscal_as_of', 'delinquent_as_of_date'}


def _get_delinquency_drill_columns():
	"""Get all columns from the delinquency drill-through materialized view."""
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
				['web_ai', 'delinquency_drill_through']
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Delinquency drill-through column introspection failed:', exc)

	if columns:
		return columns

	try:
		with connection.cursor() as cur:
			cur.execute(
				"""
				SELECT attname
				FROM pg_attribute
				WHERE attrelid = 'web_ai.delinquency_drill_through'::regclass
				  AND attnum > 0
				  AND NOT attisdropped
				ORDER BY attnum
				"""
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Delinquency drill-through pg_attribute introspection failed:', exc)
	return columns


def _format_delinquency_value(alias, value):
	"""Format delinquency drill-through values for display."""
	if value is None:
		return '--'
	if isinstance(value, (datetime, date)):
		return value.strftime('%b %d, %Y')
	if isinstance(value, Decimal):
		value = float(value)
	if alias in DELINQUENCY_CURRENCY_FIELDS:
		try:
			return f"${float(value):,.2f}"
		except Exception:
			# Avoid double-dollar when DB already returns formatted string
			s = str(value).strip()
			if '$' in s:
				return s
			try:
				s2 = s.replace(',', '').replace('\u00A0', '')
				if s2.startswith('(') and s2.endswith(')'):
					s2 = '-' + s2[1:-1]
				num = float(s2)
				return f"${num:,.2f}"
			except Exception:
				return s
	if alias in DELINQUENCY_DATE_FIELDS:
		try:
			parsed = datetime.strptime(str(value), '%Y-%m-%d').date()
			return parsed.strftime('%b %d, %Y')
		except Exception:
			return str(value)
	text = str(value).strip()
	return text or '--'


def _fetch_delinquency_filter_options(columns):
	"""Fetch distinct filter options for delinquency drill-through filters."""
	options = {}
	
	for field in DELINQUENCY_FILTER_FIELDS:
		# Support fields defined with either 'keywords' or an 'exact' column name
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		else:
			col = _find_column_by_keywords(columns, field.get('keywords', []))
		if not col:
			options[field['key']] = []
			continue
		ident = _quote_ident(col)
		sql = (
			f"SELECT DISTINCT {ident} FROM {DELINQUENCY_DRILL_VIEW} "
			f"WHERE {ident} IS NOT NULL AND TRIM({ident}::text) <> '' "
			f"ORDER BY {ident} ASC LIMIT 400"
		)
		try:
			with connection.cursor() as cur:
				cur.execute(sql)
				vals = [row[0] for row in cur.fetchall()]
		except Exception as exc:
			print(f"Delinquency drill filter options failed for {field['key']}:", exc)
			vals = []
		# Exclude Livcor from investor options
		if field['key'] == 'investor':
			vals = [v for v in vals if str(v).upper() not in ['BLACKSTONE/LIVCOR', 'LIVCOR']]

		# For fiscal month-year provide a human-friendly label while preserving
		# the raw value for form submissions. Other fields remain simple strings.
		if field['key'] == 'fiscal_as_of_month_year':
			labelled = []
			for v in vals:
				if not v:
					continue
				raw = str(v).strip()
				label = _humanize_fiscal_label(raw)
				labelled.append({'value': raw, 'label': label})
			# Sort by parsed fiscal date (year then month). Use latest-first
			try:
				labelled.sort(key=lambda it: (_parse_fiscal_to_date(it.get('value')) or date.min), reverse=True)
			except Exception:
				pass
			options[field['key']] = labelled
		else:
			options[field['key']] = [str(v).strip() for v in vals if v]
	return options


def _humanize_fiscal_label(raw_val):
	"""Turn various fiscal value formats into a readable 'Mon-YYYY' label.

	Accepts formats like '2025-10-01', '2025-10', '10/01/2025', '102025',
	'Oct-2025', 'October 2025', etc. Falls back to the original string.
	"""
	if not raw_val:
		return ''
	s = str(raw_val).strip()
	# Try several common date formats
	fmts = ['%Y-%m-%d', '%Y-%m', '%m/%d/%Y', '%m-%Y', '%b-%Y', '%B %Y', '%b %Y']
	for fmt in fmts:
		try:
			dt = datetime.strptime(s, fmt)
			return dt.strftime('%b-%Y')
		except Exception:
			continue
	# Handle MMYYYY like '102025' or '1202024'
	if re.match(r'^\d{6}$', s):
		try:
			mm = int(s[:2])
			yy = int(s[2:])
			dt = date(yy, mm, 1)
			return dt.strftime('%b-%Y')
		except Exception:
			pass
	# Fallback: return original trimmed string
	return s


def _parse_fiscal_to_date(raw_val):
	"""Parse fiscal option raw value into a date object representing the month.

	Returns a date (first of month) or None if parsing fails.
	"""
	if not raw_val:
		return None
	s = str(raw_val).strip()
	fmts = ['%Y-%m-%d', '%Y-%m', '%m/%d/%Y', '%m-%Y', '%b-%Y', '%B %Y', '%b %Y']
	for fmt in fmts:
		try:
			dt = datetime.strptime(s, fmt)
			return date(dt.year, dt.month, 1)
		except Exception:
			continue
	if re.match(r'^\d{6}$', s):
		try:
			mm = int(s[:2])
			yy = int(s[2:])
			return date(yy, mm, 1)
		except Exception:
			return None
	return None


def _fetch_delinquency_summary(filter_clauses, filter_params, columns):
	"""Calculate summary metrics for delinquency drill-through.

	Uses a SQL aggregation that strips non-numeric characters before casting
	to numeric to avoid Decimal conversion errors when the source columns
	contain formatted text (commas, dollar signs, etc.).
	"""
	summary = {
		'total_delinquent': 0,
		'total_prepaid': 0,
		'delinquency_as_of': None,
		'days_0_30': 0,
		'days_30_60': 0,
		'days_60_90': 0,
	}

	# Find relevant columns
	total_delinquent_col = _find_column_by_keywords(columns, ['total', 'delinquent'])
	total_prepaid_col = _find_column_by_keywords(columns, ['total', 'prepaid'])
	days_0_30_col = _find_exact_column(columns, '0-30_days')
	days_30_60_col = _find_exact_column(columns, '30-60_days')
	days_60_90_col = _find_exact_column(columns, '60-90_days')
	# Attempt to locate the "as of" date column. The materialized view
	# may use different names like 'delinquent_as_of_date', 'delinquent_as_of',
	# or variants using the words 'delinquency'/'delinquent'. Try several
	# exact and keyword-based matches to increase robustness.
	delinquency_as_of_col = (
		_find_exact_column(columns, 'delinquent_as_of_date')
		or _find_exact_column(columns, 'delinquent_as_of')
		or _find_exact_column(columns, 'delinquency_as_of')
		or _find_column_by_keywords(columns, ['delinquent', 'as', 'of'])
		or _find_column_by_keywords(columns, ['delinquency', 'as', 'of'])
		or _find_column_by_keywords(columns, ['delinquent', 'as'])
	)

	sql_parts = []

	def cleaned_sum_expr(col_name):
		# Convert value to text, remove non-numeric characters except . and -,
		# NULLIF empty string to avoid cast errors, then cast to numeric
		ident = _quote_ident(col_name)
		return f"SUM( (NULLIF(regexp_replace({ident}::text, '[^0-9.\-]', '', 'g'), ''))::numeric )"

	if total_delinquent_col:
		sql_parts.append(f"{cleaned_sum_expr(total_delinquent_col)} AS total_delinquent")
	if total_prepaid_col:
		sql_parts.append(f"{cleaned_sum_expr(total_prepaid_col)} AS total_prepaid")
	if days_0_30_col:
		sql_parts.append(f"{cleaned_sum_expr(days_0_30_col)} AS days_0_30")
	if days_30_60_col:
		sql_parts.append(f"{cleaned_sum_expr(days_30_60_col)} AS days_30_60")
	if days_60_90_col:
		sql_parts.append(f"{cleaned_sum_expr(days_60_90_col)} AS days_60_90")
	if delinquency_as_of_col:
		sql_parts.append(f"MAX({_quote_ident(delinquency_as_of_col)}) AS latest_date")

	if sql_parts:
		# Apply all user-selected filters to the summary aggregation
		# Note: We do NOT filter by RowNum here because we need to sum across
		# all matching rows to get accurate totals
		local_clauses = list(filter_clauses) if filter_clauses else []
		local_params = list(filter_params) if filter_params else []

		sql = f"SELECT {', '.join(sql_parts)} FROM {DELINQUENCY_DRILL_VIEW}"
		if local_clauses:
			sql += ' WHERE ' + ' AND '.join(local_clauses)

		try:
			with connection.cursor() as cur:
				cur.execute(sql, local_params)
				row = cur.fetchone()
				col_names = [desc[0] for desc in cur.description]
				data = dict(zip(col_names, row if row else []))
		except Exception as exc:
			print('Delinquency drill summary query failed:', exc)
			data = {}
	else:
		data = {}

	# Populate summary from query result
	try:
		summary['total_delinquent'] = data.get('total_delinquent') or 0
		summary['total_prepaid'] = data.get('total_prepaid') or 0
		summary['days_0_30'] = data.get('days_0_30') or 0
		summary['days_30_60'] = data.get('days_30_60') or 0
		summary['days_60_90'] = data.get('days_60_90') or 0
	except Exception:
		# Defensive: ensure numeric defaults
		summary['total_delinquent'] = summary.get('total_delinquent', 0)
		summary['total_prepaid'] = summary.get('total_prepaid', 0)

	# Add unit_count for template compatibility (uses total_count as record count)
	summary['unit_count'] = None

	latest_date = data.get('latest_date')
	if latest_date:
		try:
			if isinstance(latest_date, str):
				date_obj = datetime.strptime(latest_date, '%Y-%m-%d').date()
			else:
				date_obj = latest_date
			summary['delinquency_as_of'] = date_obj.strftime('%m/%d/%Y')
		except Exception:
			summary['delinquency_as_of'] = str(latest_date)

	return summary


def _prepare_delinquency_drill_query(request):
	"""Prepare delinquency drill-through query components."""
	columns = _get_delinquency_drill_columns()
	if not columns:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {},
				'filter_options': {},
				'filters': {},
				'error': 'The delinquency drill-through view is currently unavailable.',
			},
		}

	select_parts = []
	display_columns = []
	alias_order = []
	alias_to_source = {}
	
	for field in DELINQUENCY_TABLE_FIELDS:
		col = None
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		if not col and 'keywords' in field:
			col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			continue
		alias = field['key']
		alias_to_source[alias] = col
		alias_order.append(alias)
		display_columns.append({'key': alias, 'label': field['label']})
		quoted_alias = _quote_ident(alias) if alias and alias[0].isdigit() else alias
		select_parts.append(f"{_quote_ident(col)} AS {quoted_alias}")

	dedup_columns = list(alias_to_source.values())
	filter_options = _fetch_delinquency_filter_options(columns)

	if not select_parts:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {},
				'filter_options': filter_options,
				'filters': {},
				'error': 'No recognizable columns were found for the delinquency drill-through dataset.',
			},
		}

	filter_clauses = []
	filter_params = []
	active_filters = {}
	
	# Note: investor-based exclusions (e.g., Livcor) are applied later after
	# processing explicit filter selections so that choosing an investor like
	# 'Livcor' will return results. We intentionally do not add unconditional
	# exclusions here.
	
	for field in DELINQUENCY_FILTER_FIELDS:
		values = _extract_filter_list(request, request.GET, field['key'])
		clean = [v.strip() for v in values if v and v.strip() and v.strip().lower() != 'all'] if values else []
		active_filters[field['key']] = values[0] if values else ''
		if not clean:
			continue
		# Support exact column names when provided (useful for fiscal month-year)
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		else:
			col = _find_column_by_keywords(columns, field.get('keywords', []))
		if not col:
			continue
		clause_parts = []
		for val in clean:
			# For exact fiscal month-year match use equality, otherwise use ILIKE
			if field.get('exact'):
				clause_parts.append(f"{_quote_ident(col)} = %s")
				filter_params.append(val)
			else:
				clause_parts.append(f"{_quote_ident(col)} ILIKE %s")
				filter_params.append(f"%{val}%")
		if clause_parts:
			filter_clauses.append('(' + ' OR '.join(clause_parts) + ')')
			# If user filtered by fiscal month-year, also constrain the
			# scorecard-derived delinquent_as_of_date to the same month to
			# avoid the materialized view's cross-month join multiplying rows.
			if field.get('exact') and field['key'] == 'fiscal_as_of_month_year':
				ds_col = (
					_find_exact_column(columns, 'delinquent_as_of_date')
					or _find_exact_column(columns, 'delinquent_as_of')
				)
				if ds_col:
					ds_clause_parts = []
					for val in clean:
						parsed = _parse_fiscal_to_date(val)
						if parsed:
							ds_clause_parts.append(f"DATE_TRUNC('month', {_quote_ident(ds_col)})::date = %s")
							filter_params.append(parsed)
					if ds_clause_parts:
						filter_clauses.append('(' + ' OR '.join(ds_clause_parts) + ')')

	return {
		'error': False,
		'columns': columns,
		'display_columns': display_columns,
		'alias_order': alias_order,
		'alias_to_source': alias_to_source,
		'dedup_columns': dedup_columns,
		'select_parts': select_parts,
		'filter_clauses': filter_clauses,
		'filter_params': filter_params,
		'active_filters': active_filters,
		'filter_options': filter_options,
	}


def _build_delinquency_drill_context(request):
	"""Build complete context for delinquency drill-through view."""
	query_info = _prepare_delinquency_drill_query(request)
	if query_info.get('error'):
		return query_info['error_context']

	columns = query_info['columns']
	display_columns = query_info['display_columns']
	alias_order = query_info['alias_order']
	select_parts = query_info['select_parts']
	filter_clauses = query_info['filter_clauses']
	filter_params = query_info['filter_params']
	active_filters = query_info['active_filters']
	filter_options = query_info['filter_options']


	# If user didn't provide a fiscal month filter, attempt to default to the
	# previous available fiscal month (or the most recent available month).
	fiscal_key = 'fiscal_as_of_month_year'
	try:
		has_fiscal_filter = bool(active_filters.get(fiscal_key))
	except Exception:
		has_fiscal_filter = False
	if not has_fiscal_filter:
		fiscal_opts = filter_options.get(fiscal_key) or []
		# fiscal_opts expected to be list of dicts with 'value' and 'label'
		candidates = []
		for o in fiscal_opts:
			raw = o.get('value') if isinstance(o, dict) else o
			dt = _parse_fiscal_to_date(raw)
			if dt:
				candidates.append((dt, raw))
		if candidates:
			# target = previous calendar month
			today = datetime.now().date()
			if today.month == 1:
				target = date(today.year - 1, 12, 1)
			else:
				target = date(today.year, today.month - 1, 1)
			# Find exact match for previous month
			match = next((r for (d, r) in candidates if d.year == target.year and d.month == target.month), None)
			if not match:
				# fallback: pick latest candidate <= target, else pick latest overall
				leq = [ (d, r) for (d, r) in candidates if d <= target ]
				if leq:
					match = max(leq, key=lambda t: t[0])[1]
				else:
					match = max(candidates, key=lambda t: t[0])[1]
			# Apply chosen default to active filters and extend filter clauses/params
			if match:
				# find the actual column name for fiscal
				fcol = None
				for f in DELINQUENCY_FILTER_FIELDS:
					if f['key'] == fiscal_key:
						if 'exact' in f:
							fcol = _find_exact_column(columns, f['exact'])
						else:
							fcol = _find_column_by_keywords(columns, f.get('keywords', []))
						break
				if fcol:
					filter_clauses.append(f"{_quote_ident(fcol)} = %s")
					filter_params.append(match)
					active_filters[fiscal_key] = match
					# Also constrain the delinquent-as-of date produced by the scorecard
					# so the materialized view's join (which multiplies rows by
					# month) does not return repeated rows across all months. If we
					# can parse the fiscal match into a month date, add a month-level
					# filter on the delinquent_as_of_date column (from the scorecard).
					ds_col = (
						_find_exact_column(columns, 'delinquent_as_of_date')
						or _find_exact_column(columns, 'delinquent_as_of')
					)
					if ds_col:
						parsed = _parse_fiscal_to_date(match)
						if parsed:
							filter_clauses.append(f"DATE_TRUNC('month', {_quote_ident(ds_col)})::date = %s")
							filter_params.append(parsed)

	# Compute summary AFTER applying any default fiscal filter
	summary = _fetch_delinquency_summary(filter_clauses, filter_params, columns)
	
	# Count total matching records for pagination
	count_sql = f"SELECT COUNT(*) FROM {DELINQUENCY_DRILL_VIEW}"
	if filter_clauses:
		count_sql += ' WHERE ' + ' AND '.join(filter_clauses)
	
	try:
		with connection.cursor() as cur:
			cur.execute(count_sql, filter_params)
			total_records = cur.fetchone()[0] or 0
	except Exception as exc:
		print('Delinquency drill-through count query failed:', exc)
		total_records = 0
	
	page_size = DELINQUENCY_DRILL_PAGE_SIZE
	page_param = request.GET.get('page') if hasattr(request, 'GET') else None
	try:
		requested_page = int(page_param) if page_param else 1
	except Exception:
		requested_page = 1
	if requested_page < 1:
		requested_page = 1
	if total_records:
		total_pages = (total_records + page_size - 1) // page_size
		page = min(requested_page, total_pages)
	else:
		total_pages = 1
		page = 1
	offset = (page - 1) * page_size if total_records else 0

	where_sql = ' WHERE ' + ' AND '.join(filter_clauses) if filter_clauses else ''
	order_alias = 'property_name' if 'property_name' in alias_order else (alias_order[0] if alias_order else None)
	data_sql = f"SELECT {', '.join(select_parts)} FROM {DELINQUENCY_DRILL_VIEW}{where_sql}"
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
		print('Delinquency drill-through data query failed:', exc)
		result = []
	
	for raw in result:
		row_dict = dict(zip(alias_order, raw))
		ordered_values = [_format_delinquency_value(alias, row_dict.get(alias)) for alias in alias_order]
		rows.append(ordered_values)

	start_index = offset + 1 if total_records and rows else 0
	end_index = offset + len(rows)
	if total_records and end_index > total_records:
		end_index = total_records

	base_query_pairs = []
	if hasattr(request.GET, 'lists'):
		for key, values in request.GET.lists():
			if key == 'page':
				continue
			for val in values:
				if val:
					base_query_pairs.append((key, val))
	
	dashboard_return_url = request.GET.get('return_url') or reverse('dashboard')
	if base_query_pairs:
		base_query_pairs.append(('return_url', dashboard_return_url))
	
	def build_page_url(pg):
		pairs = list(base_query_pairs)
		pairs.append(('page', pg))
		return f"{reverse('delinquency_drillthrough')}?{urlencode(pairs, doseq=True)}"
	
	prev_url = build_page_url(page - 1) if page > 1 else None
	next_url = build_page_url(page + 1) if page < total_pages else None

	export_pairs = list(base_query_pairs)
	export_base = reverse('delinquency_drillthrough_export')
	if export_pairs:
		export_url = f"{export_base}?{urlencode(export_pairs, doseq=True)}"
	else:
		export_url = export_base

	reset_base = reverse('delinquency_drillthrough')
	if request.GET.get('return_url'):
		drill_reset_url = f"{reset_base}?{urlencode({'return_url': request.GET.get('return_url')})}"
	else:
		drill_reset_url = reset_base

	filter_fields = [{'key': field['key'], 'label': field['label']} for field in DELINQUENCY_FILTER_FIELDS]
	filter_blocks = []
	for field in filter_fields:
		key = field['key']
		filter_blocks.append({
			'key': key,
			'label': field['label'],
			'options': filter_options.get(key, []),
			'selected': active_filters.get(key, ''),
		})

	# Build chart data: delinquency trend (monthly) and aged receivables breakdown
	try:
		# Find suitable date and amount columns. Prefer the actual
		# `delinquent_as_of_date` (represents when the receivable is
		# delinquent) so the trend reflects delinquent-as-of totals rather
		# than fiscal-assigned months which can inflate sums.
		date_col = (
			_find_exact_column(columns, 'delinquent_as_of_date')
			or _find_exact_column(columns, 'delinquent_as_of')
			or _find_exact_column(columns, 'fiscal_as_of_month_year')
			or _find_column_by_keywords(columns, ['delinquent', 'as', 'of'])
			or _find_column_by_keywords(columns, ['fiscal', 'as', 'of'])
		)
		total_col = _find_column_by_keywords(columns, ['total', 'delinquent'])
		# Build trend SQL: sum total_delinquent by month, respecting active filters
		trend = {'labels': [], 'values': []}
		if date_col and total_col:
			# The underlying materialized view can contain repeated rows per
			# property-month; to avoid inflated totals, first reduce to one row
			# per property+month using the view's scorecard column
			# `total_delinquent_as_of_date` when present, then aggregate. For the
			# trend we honour property/region filters but ignore the selected
			# fiscal month so that the chart always shows the latest six months.
			amt_as_of_col = _find_exact_column(columns, 'total_delinquent_as_of_date')
			trend_clauses = []
			trend_params = []
			param_idx = 0
			for clause in filter_clauses:
				placeholder_count = clause.count('%s')
				params_slice = filter_params[param_idx:param_idx + placeholder_count]
				param_idx += placeholder_count
				clause_lower = clause.lower()
				if 'fiscal as of month year' in clause_lower or 'date_trunc(' in clause_lower:
					continue
				trend_clauses.append(clause)
				trend_params.extend(params_slice)
			current_today = datetime.now().date()
			current_month_start = date(current_today.year, current_today.month, 1)
			trend_clauses.append(f"DATE_TRUNC('month', {_quote_ident(date_col)})::date < %s")
			trend_params.append(current_month_start)
			trend_where = f" WHERE {_quote_ident(date_col)} IS NOT NULL"
			if trend_clauses:
				trend_where += ' AND ' + ' AND '.join(trend_clauses)
			if amt_as_of_col:
				trend_sql = (
					"SELECT month_start, SUM(total_sum) AS total_sum FROM ("
					f" SELECT DISTINCT ON (property_name, DATE_TRUNC('month', {_quote_ident(date_col)})::date) "
					f" property_name, DATE_TRUNC('month', {_quote_ident(date_col)})::date AS month_start, "
					f" (NULLIF(REGEXP_REPLACE({_quote_ident(amt_as_of_col)}::text, '[^0-9.\\-]', '', 'g'), ''))::numeric AS total_sum "
					f" FROM {DELINQUENCY_DRILL_VIEW}{trend_where} "
					f" ORDER BY property_name, month_start DESC) s "
					f"GROUP BY month_start ORDER BY month_start DESC LIMIT 6")
			else:
				trend_sql = (
					"SELECT month_start, SUM(total_sum) AS total_sum FROM ("
					f" SELECT DISTINCT ON (property_name, DATE_TRUNC('month', {_quote_ident(date_col)})::date) "
					f" property_name, DATE_TRUNC('month', {_quote_ident(date_col)})::date AS month_start, "
					f" (NULLIF(REGEXP_REPLACE({_quote_ident(total_col)}::text, '[^0-9.\\-]', '', 'g'), ''))::numeric AS total_sum "
					f" FROM {DELINQUENCY_DRILL_VIEW}{trend_where} "
					f" ORDER BY property_name, month_start DESC) s "
					f"GROUP BY month_start ORDER BY month_start DESC LIMIT 6")
			try:
				with connection.cursor() as cur:
					cur.execute(trend_sql, trend_params)
					trend_rows = cur.fetchall()
					# trend_rows are newest-first; reverse for chronological order
					trend_rows = list(trend_rows)[::-1]
					for r in trend_rows:
						m = r[0]
						val = r[1] or 0
						trend['labels'].append(m.strftime('%b-%Y') if hasattr(m, 'strftime') else str(m))
						trend['values'].append(float(val))
			except Exception as exc:
				print('Delinquency trend query failed:', exc)
		# Aged receivables: dedupe by property and use filtered data
		# Find bucket columns - use exact names with spaces as they appear in the view
		days_0_30_col = _find_exact_column(columns, '0-30 Days')
		days_30_60_col = _find_exact_column(columns, '30-60 Days')
		days_60_90_col = _find_exact_column(columns, '60-90 Days')
		amt_as_of_col = _find_exact_column(columns, 'total_delinquent_as_of_date')
		
		aged = {'labels': [], 'values': [], 'total': 0, 'month': ''}
		if date_col and amt_as_of_col and days_0_30_col and days_30_60_col and days_60_90_col:
			# Build deduped aged query for the filtered data
			where_clause = ''
			if filter_clauses:
				where_clause = ' AND ' + ' AND '.join(filter_clauses)
			total_col = _find_exact_column(columns, 'total_delinquent') or _find_exact_column(columns, 'total_delinquent_as_of_date')
			total_expr = f"(NULLIF(REGEXP_REPLACE({_quote_ident(total_col)}::text, '[^0-9.\\-]', '', 'g'), ''))::numeric" if total_col else '0'
			aged_sql = (
				"SELECT "
				f"  MAX(DATE_TRUNC('month', {_quote_ident(date_col)})::date) AS month_start, "
				f"  SUM({total_expr}) AS total_delinquent, "
				f"  SUM((NULLIF(REGEXP_REPLACE({_quote_ident(days_0_30_col)}::text, '[^0-9.\\-]', '', 'g'), ''))::numeric) AS days_0_30, "
				f"  SUM((NULLIF(REGEXP_REPLACE({_quote_ident(days_30_60_col)}::text, '[^0-9.\\-]', '', 'g'), ''))::numeric) AS days_30_60, "
				f"  SUM((NULLIF(REGEXP_REPLACE({_quote_ident(days_60_90_col)}::text, '[^0-9.\\-]', '', 'g'), ''))::numeric) AS days_60_90 "
				f"FROM {DELINQUENCY_DRILL_VIEW} "
				f"WHERE {_quote_ident(date_col)} IS NOT NULL{where_clause}"
			)
			try:
				with connection.cursor() as cur:
					cur.execute(aged_sql, filter_params)
					aged_row = cur.fetchone()
					if aged_row:
						month_start, total_del, d_0_30, d_30_60, d_60_90 = aged_row
						aged['month'] = (
							month_start.strftime('%b-%Y') if hasattr(month_start, 'strftime') else str(month_start)
						) if month_start else (
							active_filters.get('fiscal_as_of_month_year') or summary.get('delinquency_as_of') or 'Latest'
						)
						aged['total'] = float(total_del or 0)
						aged['labels'] = ['Total Delinquent', '0-30 Days', '30-60 Days', '60-90 Days']
						aged['values'] = [
							float(total_del or 0),
							float(d_0_30 or 0),
							float(d_30_60 or 0),
							float(d_60_90 or 0)
						]
			except Exception as exc:
				print('Delinquency aged receivables query failed:', exc)
				import traceback
				traceback.print_exc()
		else:
			# Fallback to summary if columns not found
			aged = {
				'labels': ['Total Delinquent', '0-30 Days', '30-60 Days', '60-90 Days'],
				'values': [
					float(summary.get('total_delinquent') or 0),
					float(summary.get('days_0_30') or 0),
					float(summary.get('days_30_60') or 0),
					float(summary.get('days_60_90') or 0),
				],
				'total': float(summary.get('total_delinquent') or 0),
				'month': active_filters.get('fiscal_as_of_month_year') or summary.get('delinquency_as_of') or ''
			}
		chart_payload = {
			'trend': trend,
			'aged': aged,
			'label': active_filters.get('fiscal_as_of_month_year') or summary.get('delinquency_as_of') or ''
		}
		chart_data = json.dumps(chart_payload)
	except Exception:
		chart_data = json.dumps({'trend': {'labels': [], 'values': []}, 'aged': {'labels': [], 'values': [], 'total': 0}, 'label': ''})

	return {
		'table_columns': display_columns,
		'table_rows': rows,
		'metrics': summary,
		'filters': active_filters,
		'filter_options': filter_options,
		'filter_blocks': filter_blocks,
		'pagination': {
			'page': page,
			'total_pages': total_pages,
			'total_count': total_records,
			'start_index': start_index,
			'end_index': end_index,
			'has_prev': page > 1,
			'has_next': page < total_pages,
			'prev_url': prev_url,
			'next_url': next_url,
		},
		'dashboard_return_url': dashboard_return_url,
		'drill_reset_url': drill_reset_url,
		'export_url': export_url,
		'chart_data': chart_data,
	}


@login_required
def delinquency_drillthrough(request):
	"""Main delinquency drill-through view."""
	context = _build_delinquency_drill_context(request)
	context['filter_fields'] = [{'key': field['key'], 'label': field['label']} for field in DELINQUENCY_FILTER_FIELDS]
	context['page_title'] = 'Delinquency Drill-Through'
	context['row_limit'] = DELINQUENCY_DRILL_PAGE_SIZE
	context.setdefault('error', '')
	context.setdefault('export_url', '')
	
	return render(request, 'dashboard/delinquency_drillthrough.html', context)


@login_required
def delinquency_drillthrough_export(request):
	"""CSV export for delinquency drill-through."""
	query_info = _prepare_delinquency_drill_query(request)
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
	data_sql = f"SELECT {', '.join(select_parts)} FROM {DELINQUENCY_DRILL_VIEW}{where_sql}"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} NULLS LAST"

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, list(filter_params))
			rows = cur.fetchall()
	except Exception as exc:
		print('Delinquency drill-through export failed:', exc)
		return HttpResponse('Failed to export data.', status=500)

	timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
	filename = f"delinquency_drillthrough_{timestamp}.csv"
	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = f'attachment; filename="{filename}"'

	writer = csv.writer(response)
	writer.writerow([col['label'] for col in display_columns])
	for raw in rows:
		row_dict = dict(zip(alias_order, raw))
		writer.writerow([_format_delinquency_value(alias, row_dict.get(alias)) for alias in alias_order])

	return response


# ============================================================================
# SERVICE REQUEST DRILL-THROUGH VIEWS
# ============================================================================

SERVICE_REQUEST_DRILL_VIEW = 'web_ai.service_request_drill_through'
SERVICE_REQUEST_DRILL_PAGE_SIZE = 500

SERVICE_REQUEST_FILTER_FIELDS = [
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'keywords': ['regional', 'vp']},
	{'key': 'regional_manager', 'label': 'Regional Manager', 'keywords': ['regional', 'area', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
]

SERVICE_REQUEST_TABLE_FIELDS = [
	{'key': 'property_name', 'label': 'Property Name', 'keywords': ['property', 'name']},
	{'key': 'request_number', 'label': 'Request Number', 'keywords': ['request', 'number']},
	{'key': 'unit_number', 'label': 'Unit Number', 'keywords': ['unit', 'number']},
	{'key': 'created_date', 'label': 'Created Date', 'keywords': ['created', 'date']},
	{'key': 'completed_date_time', 'label': 'Completed Date', 'keywords': ['completed', 'date']},
	{'key': 'days_open', 'label': 'Days Open', 'keywords': ['days', 'open']},
	{'key': 'requestor', 'label': 'Requestor', 'keywords': ['requestor']},
	{'key': 'category', 'label': 'Category', 'keywords': ['category']},
	{'key': 'item', 'label': 'Item', 'keywords': ['item']},
	{'key': 'issue', 'label': 'Issue', 'keywords': ['issue']},
	{'key': 'assigned_to', 'label': 'Assigned To', 'keywords': ['assigned', 'to']},
	{'key': 'status', 'label': 'Status', 'keywords': ['status']},
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'exact': 'Regional VP  | Sr. VP'},
	{'key': 'regional_area_manager', 'label': 'Regional Area Manager', 'keywords': ['regional', 'area', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
]

SERVICE_REQUEST_DATE_FIELDS = {'created_date', 'completed_date_time'}


def _get_service_request_drill_columns():
	"""Get all columns from the service request drill-through materialized view."""
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
				['web_ai', 'service_request_drill_through']
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Service request drill-through column introspection failed:', exc)

	if columns:
		return columns

	try:
		with connection.cursor() as cur:
			cur.execute(
				"""
				SELECT attname
				FROM pg_attribute
				WHERE attrelid = 'web_ai.service_request_drill_through'::regclass
				  AND attnum > 0
				  AND NOT attisdropped
				ORDER BY attnum
				"""
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Service request drill-through pg_attribute introspection failed:', exc)
	return columns


def _format_service_request_value(alias, value):
	"""Format service request drill-through values for display."""
	if value is None:
		return '--'
	if isinstance(value, (datetime, date)):
		return value.strftime('%b %d, %Y')
	if alias in SERVICE_REQUEST_DATE_FIELDS:
		try:
			parsed = datetime.strptime(str(value), '%Y-%m-%d').date()
			return parsed.strftime('%b %d, %Y')
		except Exception:
			return str(value)
	text = str(value).strip()
	return text or '--'


def _fetch_service_request_filter_options(columns):
	"""Fetch distinct filter options for service request drill-through filters."""
	options = {}
	
	for field in SERVICE_REQUEST_FILTER_FIELDS:
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		else:
			col = _find_column_by_keywords(columns, field.get('keywords', []))
		if not col:
			options[field['key']] = []
			continue
		ident = _quote_ident(col)
		sql = (
			f"SELECT DISTINCT {ident} FROM {SERVICE_REQUEST_DRILL_VIEW} "
			f"WHERE {ident} IS NOT NULL AND TRIM({ident}::text) <> '' "
			f"ORDER BY {ident} ASC LIMIT 400"
		)
		try:
			with connection.cursor() as cur:
				cur.execute(sql)
				vals = [row[0] for row in cur.fetchall()]
		except Exception as exc:
			print(f"Service request drill filter options failed for {field['key']}:", exc)
			vals = []
		# Exclude Livcor from investor options
		if field['key'] == 'investor':
			vals = [v for v in vals if str(v).upper() not in ['BLACKSTONE/LIVCOR', 'LIVCOR']]
		options[field['key']] = [str(v).strip() for v in vals if v]
	return options


def _fetch_service_request_summary(filter_clauses, filter_params, columns, dedup_source_sql=None):
	"""Calculate summary metrics for service request drill-through."""
	summary = {
		'total_requests': 0,
		'completed_percentage': 0,
		'avg_time_spent': 0,
		'completed_mobile_percentage': 0,
	}

	# Create local copies of filter clauses and params for use throughout function
	local_clauses = list(filter_clauses) if filter_clauses else []
	local_params = list(filter_params) if filter_params else []

	if dedup_source_sql:
		source_sql = dedup_source_sql
	else:
		source_sql = f"SELECT DISTINCT * FROM {SERVICE_REQUEST_DRILL_VIEW}"
		if local_clauses:
			source_sql += ' WHERE ' + ' AND '.join(local_clauses)

	# Find relevant columns
	status_col = _find_column_by_keywords(columns, ['status'])
	completed_date_col = _find_column_by_keywords(columns, ['completed', 'date'])
	avg_time_spent_col = _find_column_by_keywords(columns, ['avg', 'time', 'spent'])

	sql_parts = []
	sql_parts.append("COUNT(*) AS total_requests")
	
	# Calculate completion percentage
	if completed_date_col:
		sql_parts.append(f"SUM(CASE WHEN {_quote_ident(completed_date_col)} IS NOT NULL THEN 1 ELSE 0 END) AS completed_count")
	elif status_col:
		sql_parts.append(f"SUM(CASE WHEN {_quote_ident(status_col)}::text ILIKE '%complete%' THEN 1 ELSE 0 END) AS completed_count")
	
	# Average time spent (from avg_time_spent column or days_open)
	if avg_time_spent_col:
		sql_parts.append(f"AVG(CASE WHEN {_quote_ident(avg_time_spent_col)} IS NOT NULL THEN {_quote_ident(avg_time_spent_col)} ELSE NULL END) AS avg_time")
	else:
		days_open_col = _find_column_by_keywords(columns, ['days', 'open'])
		if days_open_col:
			sql_parts.append(f"AVG(CASE WHEN {_quote_ident(days_open_col)} IS NOT NULL THEN {_quote_ident(days_open_col)} ELSE NULL END) AS avg_time")

	if sql_parts:

		sql = f"SELECT {', '.join(sql_parts)} FROM ({source_sql}) sr"

		try:
			with connection.cursor() as cur:
				cur.execute(sql, local_params)
				row = cur.fetchone()
				col_names = [desc[0] for desc in cur.description]
				data = dict(zip(col_names, row if row else []))
		except Exception as exc:
			print('Service request drill summary query failed:', exc)
			data = {}
	else:
		data = {}

	total = data.get('total_requests') or 0
	completed = data.get('completed_count') or 0
	
	summary['total_requests'] = total
	summary['completed_percentage'] = round((completed / total * 100) if total else 0, 0)
	summary['avg_time_spent'] = round(data.get('avg_time') or 0, 2)
	
	# Calculate mobile percentage using completing_system CONTAINSSTRING logic
	# DAX formula: CONTAINSSTRING(lower(completing_system), "facilities plus")
	completing_system_col = _find_column_by_keywords(columns, ['completing', 'system'])
	unique_key_col = _find_column_by_keywords(columns, ['unique', 'key'])
	
	if completing_system_col and unique_key_col:
		# Note: %% is used to escape % in psycopg2/Django SQL (% is parameter placeholder)
		mobile_sql = f"""
			SELECT 
				COUNT(DISTINCT {_quote_ident(unique_key_col)}) as total_unique,
				COUNT(DISTINCT CASE 
					WHEN LOWER({_quote_ident(completing_system_col)}::text) LIKE '%%facilities plus%%' 
					THEN {_quote_ident(unique_key_col)} 
				END) as mobile_unique
			FROM ({source_sql}) sr
		"""
		
		try:
			with connection.cursor() as cur:
				cur.execute(mobile_sql, local_params)
				mobile_row = cur.fetchone()
				total_unique = mobile_row[0] or 0
				mobile_unique = mobile_row[1] or 0
				summary['completed_mobile_percentage'] = round((mobile_unique / total_unique * 100) if total_unique else 0, 0)
		except Exception as exc:
			print('Service request mobile percentage calculation failed:', exc)
			summary['completed_mobile_percentage'] = 0
	else:
		summary['completed_mobile_percentage'] = 0

	return summary


def _fetch_service_request_chart_data(columns, filter_clauses, filter_params, dedup_source_sql=None):
		"""Assemble chart payload for service request drill-through charts."""
		chart_payload = {
			'category_breakdown': {
				'labels': [],
				'values': []
			},
			'move_in_within_five_days': {
				'labels': [],
				'values': []
			}
		}
		category_col = _find_column_by_keywords(columns, ['category'])
		created_col = _find_column_by_keywords(columns, ['created', 'date']) or _find_column_by_keywords(columns, ['created'])
		move_in_col = (
			_find_column_by_keywords(columns, ['move', 'in', 'date'])
			or _find_exact_column(columns, 'Move In Date')
			or _find_exact_column(columns, 'Move-In Date')
			or _find_exact_column(columns, 'Move-in Date')
			or _find_exact_column(columns, 'move_in_date')
			or _find_exact_column(columns, 'Move In Date Time')
			or _find_exact_column(columns, 'move_in_date_time')
			or _find_column_by_keywords(columns, ['move', 'in'])
		)

		if dedup_source_sql:
			base_sql = dedup_source_sql
		else:
			base_sql = f"SELECT DISTINCT * FROM {SERVICE_REQUEST_DRILL_VIEW}"
			if filter_clauses:
				base_sql += ' WHERE ' + ' AND '.join(filter_clauses)

		has_data = False

		if category_col:
			cat_ident = _quote_ident(category_col)
			category_sql = (
				"SELECT "
				f"{cat_ident} AS category_label, COUNT(*)::bigint AS total_count "
				f"FROM ({base_sql}) sr "
				f"WHERE {cat_ident} IS NOT NULL AND TRIM({cat_ident}::text) <> '' "
				f"GROUP BY {cat_ident} "
				"ORDER BY total_count DESC LIMIT 12"
			)
			try:
				with connection.cursor() as cur:
					cur.execute(category_sql, filter_params)
					rows = cur.fetchall()
					for label, total in rows:
						chart_payload['category_breakdown']['labels'].append(str(label))
						chart_payload['category_breakdown']['values'].append(float(total or 0))
					if rows:
						has_data = True
			except Exception as exc:
				print('Service request category chart query failed:', exc)

		if category_col and created_col and move_in_col:
			cat_ident = _quote_ident(category_col)
			created_ident = _quote_ident(created_col)
			move_in_ident = _quote_ident(move_in_col)
			move_sql = (
				"SELECT "
				f"{cat_ident} AS category_label, COUNT(*)::bigint AS total_count "
				f"FROM ({base_sql}) sr "
				f"WHERE {cat_ident} IS NOT NULL AND TRIM({cat_ident}::text) <> '' "
				f"AND {created_ident} IS NOT NULL AND {move_in_ident} IS NOT NULL "
				f"AND ABS(({created_ident}::date - {move_in_ident}::date)) <= 5 "
				f"GROUP BY {cat_ident} "
				"ORDER BY total_count DESC LIMIT 12"
			)
			try:
				with connection.cursor() as cur:
					cur.execute(move_sql, filter_params)
					rows = cur.fetchall()
					for label, total in rows:
						chart_payload['move_in_within_five_days']['labels'].append(str(label))
						chart_payload['move_in_within_five_days']['values'].append(float(total or 0))
					if rows:
						has_data = True
			except Exception as exc:
				print('Service request move-in chart query failed:', exc)

		return json.dumps(chart_payload), has_data


def _prepare_service_request_drill_query(request):
	"""Prepare service request drill-through query components."""
	columns = _get_service_request_drill_columns()
	if not columns:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {},
				'filter_options': {},
				'filters': {},
				'criteria': 'created',
				'error': 'The service request drill-through view is currently unavailable.',
			},
		}

	select_parts = []
	display_columns = []
	alias_order = []
	alias_to_source = {}
	
	for field in SERVICE_REQUEST_TABLE_FIELDS:
		col = None
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		if not col and 'keywords' in field:
			col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			continue
		alias = field['key']
		alias_to_source[alias] = col
		alias_order.append(alias)
		display_columns.append({'key': alias, 'label': field['label']})
		quoted_alias = _quote_ident(alias) if alias and alias[0].isdigit() else alias
		select_parts.append(f"{_quote_ident(col)} AS {quoted_alias}")

	filter_options = _fetch_service_request_filter_options(columns)

	if not select_parts:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {},
				'filter_options': filter_options,
				'filters': {},
				'error': 'No recognizable columns were found for the service request drill-through dataset.',
			},
		}

	dedup_columns = []
	dedup_seen = set()
	for src in alias_to_source.values():
		if src and src not in dedup_seen:
			dedup_columns.append(src)
			dedup_seen.add(src)

	request_number_col = _find_column_by_keywords(columns, ['request', 'number'])
	unique_key_col = _find_column_by_keywords(columns, ['unique', 'key'])
	category_col = _find_column_by_keywords(columns, ['category'])
	item_col = _find_column_by_keywords(columns, ['item'])
	created_col = _find_column_by_keywords(columns, ['created', 'date']) or _find_column_by_keywords(columns, ['created'])
	completed_col = _find_column_by_keywords(columns, ['completed', 'date']) or _find_column_by_keywords(columns, ['completed'])
	move_in_col = (_find_column_by_keywords(columns, ['move', 'in', 'date'])
		or _find_exact_column(columns, 'Move In Date')
		or _find_exact_column(columns, 'Move-In Date')
		or _find_exact_column(columns, 'Move-in Date')
		or _find_exact_column(columns, 'move_in_date')
		or _find_exact_column(columns, 'Move In Date Time')
		or _find_exact_column(columns, 'move_in_date_time')
		or _find_column_by_keywords(columns, ['move', 'in']))
	avg_time_spent_col = _find_column_by_keywords(columns, ['avg', 'time', 'spent'])
	completing_system_col = _find_column_by_keywords(columns, ['completing', 'system'])
	investor_col = _find_column_by_keywords(columns, ['investor'])

	for extra in [
		request_number_col,
		unique_key_col,
		category_col,
		item_col,
		created_col,
		completed_col,
		move_in_col,
		avg_time_spent_col,
		completing_system_col,
		investor_col,
	]:
		if extra and extra not in dedup_seen:
			dedup_columns.append(extra)
			dedup_seen.add(extra)

	filter_clauses = []
	filter_params = []
	active_filters = {}
	
	# Exclude Livcor properties
	if investor_col:
		investor_ident = _quote_ident(investor_col)
		filter_clauses.append(f"({investor_ident} IS NULL OR ({investor_ident}::text NOT ILIKE %s AND {investor_ident}::text NOT ILIKE %s))")
		filter_params.extend(['%BLACKSTONE/LIVCOR%', '%LIVCOR%'])
	
	# Handle criteria filter (created vs completed)
	criteria = request.GET.get('criteria', 'created').strip().lower()
	if criteria not in ['created', 'completed']:
		criteria = 'created'
	active_filters['criteria'] = criteria

	# Handle date range filters
	start_date = request.GET.get('start_date', '').strip()
	end_date = request.GET.get('end_date', '').strip()
	
	if criteria == 'created':
		date_col = _find_column_by_keywords(columns, ['created', 'date'])
	else:
		date_col = _find_column_by_keywords(columns, ['completed', 'date'])
	
	if date_col:
		# If no dates provided, default to current month's data up to latest available date
		if not start_date and not end_date:
			from datetime import date as _date
			today = _date.today()
			first_of_month = _date(today.year, today.month, 1)
			# Query the latest available date for this column within current month
			max_sql = f"SELECT MAX({_quote_ident(date_col)}) FROM {SERVICE_REQUEST_DRILL_VIEW} WHERE {_quote_ident(date_col)} >= %s"
			try:
				with connection.cursor() as cur:
					cur.execute(max_sql, [first_of_month])
					max_row = cur.fetchone()
					max_val = max_row[0] if max_row else None
			except Exception:
				max_val = None
			# Determine end_date: use max_val if present and not in the future, otherwise use today
			if max_val:
				# max_val may be date or datetime
				try:
					max_date_only = max_val.date()
				except Exception:
					max_date_only = max_val
				if max_date_only and max_date_only <= today:
					end_date_eff = max_date_only
				else:
					end_date_eff = today
			else:
				end_date_eff = today
			# Format as ISO date strings
			start_date = first_of_month.isoformat()
			end_date = end_date_eff.isoformat()
			filter_clauses.append(f"{_quote_ident(date_col)} >= %s")
			filter_params.append(start_date)
			active_filters['start_date'] = start_date
			filter_clauses.append(f"{_quote_ident(date_col)} <= %s")
			filter_params.append(end_date)
			active_filters['end_date'] = end_date
		else:
			if start_date:
				filter_clauses.append(f"{_quote_ident(date_col)} >= %s")
				filter_params.append(start_date)
				active_filters['start_date'] = start_date
			if end_date:
				filter_clauses.append(f"{_quote_ident(date_col)} <= %s")
				filter_params.append(end_date)
				active_filters['end_date'] = end_date
	
	for field in SERVICE_REQUEST_FILTER_FIELDS:
		values = _extract_filter_list(request, request.GET, field['key'])
		clean = [v.strip() for v in values if v and v.strip() and v.strip().lower() != 'all'] if values else []
		active_filters[field['key']] = values[0] if values else ''
		if not clean:
			continue
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		else:
			col = _find_column_by_keywords(columns, field.get('keywords', []))
		if not col:
			continue
		clause_parts = []
		for val in clean:
			clause_parts.append(f"{_quote_ident(col)} ILIKE %s")
			filter_params.append(f"%{val}%")
		if clause_parts:
			filter_clauses.append('(' + ' OR '.join(clause_parts) + ')')
	
	# Handle drill_category filter for table filtering
	drill_category = request.GET.get('drill_category', '').strip()
	if drill_category:
		category_col = _find_column_by_keywords(columns, ['category'])
		if category_col:
			filter_clauses.append(f"{_quote_ident(category_col)} ILIKE %s")
			filter_params.append(f"%{drill_category}%")
			active_filters['drill_category'] = drill_category

	return {
		'error': False,
		'columns': columns,
		'display_columns': display_columns,
		'alias_order': alias_order,
		'alias_to_source': alias_to_source,
		'dedup_columns': dedup_columns,
		'select_parts': select_parts,
		'filter_clauses': filter_clauses,
		'filter_params': filter_params,
		'active_filters': active_filters,
		'filter_options': filter_options,
	}


def _build_service_request_drill_context(request):
	"""Build complete context for service request drill-through view."""
	query_info = _prepare_service_request_drill_query(request)
	if query_info.get('error'):
		return query_info['error_context']

	columns = query_info['columns']
	display_columns = query_info['display_columns']
	alias_order = query_info['alias_order']
	select_parts = query_info['select_parts']
	filter_clauses = query_info['filter_clauses']
	filter_params = query_info['filter_params']
	active_filters = query_info['active_filters']
	filter_options = query_info['filter_options']
	dedup_columns = query_info.get('dedup_columns', [])
	distinct_column_idents = [_quote_ident(col) for col in dedup_columns if col]
	if distinct_column_idents:
		dedup_source_sql = (
			f"SELECT DISTINCT {', '.join(distinct_column_idents)} FROM {SERVICE_REQUEST_DRILL_VIEW}"
		)
	else:
		dedup_source_sql = f"SELECT DISTINCT * FROM {SERVICE_REQUEST_DRILL_VIEW}"
	if filter_clauses:
		dedup_source_sql += ' WHERE ' + ' AND '.join(filter_clauses)

	summary = _fetch_service_request_summary(filter_clauses, filter_params, columns, dedup_source_sql)
	chart_data, chart_has_data = _fetch_service_request_chart_data(columns, filter_clauses, filter_params, dedup_source_sql)
	
	# Count total matching records for pagination
	count_sql = f"SELECT COUNT(*) FROM ({dedup_source_sql}) distinct_rows"
	
	try:
		with connection.cursor() as cur:
			cur.execute(count_sql, filter_params)
			total_records = cur.fetchone()[0] or 0
	except Exception as exc:
		print('Service request drill-through count query failed:', exc)
		total_records = 0
	
	page_size = SERVICE_REQUEST_DRILL_PAGE_SIZE
	page_param = request.GET.get('page') if hasattr(request, 'GET') else None
	try:
		requested_page = int(page_param) if page_param else 1
	except Exception:
		requested_page = 1
	if requested_page < 1:
		requested_page = 1
	if total_records:
		total_pages = (total_records + page_size - 1) // page_size
		page = min(requested_page, total_pages)
	else:
		total_pages = 1
		page = 1
	offset = (page - 1) * page_size if total_records else 0

	order_alias = 'created_date' if 'created_date' in alias_order else (alias_order[0] if alias_order else None)
	data_sql = f"SELECT {', '.join(select_parts)} FROM ({dedup_source_sql}) distinct_rows"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} DESC NULLS LAST"
	data_sql += " LIMIT %s OFFSET %s"
	data_params = list(filter_params) + [page_size, offset]

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, data_params)
			result = cur.fetchall()
	except Exception as exc:
		print('Service request drill-through data query failed:', exc)
		result = []
	
	for raw in result:
		row_dict = dict(zip(alias_order, raw))
		ordered_values = [_format_service_request_value(alias, row_dict.get(alias)) for alias in alias_order]
		rows.append(ordered_values)

	start_index = offset + 1 if total_records and rows else 0
	end_index = offset + len(rows)
	if total_records and end_index > total_records:
		end_index = total_records

	base_query_pairs = []
	if hasattr(request.GET, 'lists'):
		for key, values in request.GET.lists():
			if key == 'page':
				continue
			for val in values:
				if val:
					base_query_pairs.append((key, val))
	
	dashboard_return_url = request.GET.get('return_url') or reverse('dashboard')
	if base_query_pairs:
		base_query_pairs.append(('return_url', dashboard_return_url))
	
	def build_page_url(pg):
		pairs = list(base_query_pairs)
		pairs.append(('page', pg))
		return f"{reverse('service_request_drillthrough')}?{urlencode(pairs, doseq=True)}"
	
	prev_url = build_page_url(page - 1) if page > 1 else None
	next_url = build_page_url(page + 1) if page < total_pages else None

	export_pairs = list(base_query_pairs)
	export_base = reverse('service_request_drillthrough_export')
	if export_pairs:
		export_url = f"{export_base}?{urlencode(export_pairs, doseq=True)}"
	else:
		export_url = export_base

	reset_base = reverse('service_request_drillthrough')
	if request.GET.get('return_url'):
		drill_reset_url = f"{reset_base}?{urlencode({'return_url': request.GET.get('return_url')})}"
	else:
		drill_reset_url = reset_base

	filter_fields = [{'key': field['key'], 'label': field['label']} for field in SERVICE_REQUEST_FILTER_FIELDS]
	filter_blocks = []
	for field in filter_fields:
		key = field['key']
		filter_blocks.append({
			'key': key,
			'label': field['label'],
			'options': filter_options.get(key, []),
			'selected': active_filters.get(key, ''),
		})

	# No need to build HTML string - criteria and date filters are now in template
	# Just ensure filter values are in the context

	return {
		'table_columns': display_columns,
		'table_rows': rows,
		'metrics': summary,
		'filters': {
			**active_filters,
			'criteria': active_filters.get('criteria', 'created'),
			'start_date': active_filters.get('start_date', ''),
			'end_date': active_filters.get('end_date', ''),
		},
		'filter_options': filter_options,
		'filter_blocks': filter_blocks,
		'chart_data': chart_data,
		'chart_has_data': chart_has_data,
		'columns': columns,
		'pagination': {
			'page': page,
			'total_pages': total_pages,
			'total_count': total_records,
			'start_index': start_index,
			'end_index': end_index,
			'has_prev': page > 1,
			'has_next': page < total_pages,
			'prev_url': prev_url,
			'next_url': next_url,
		},
		'dashboard_return_url': dashboard_return_url,
		'drill_reset_url': drill_reset_url,
		'export_url': export_url,
	}


@login_required
def service_request_drillthrough(request):
	"""Main service request drill-through view."""
	context = _build_service_request_drill_context(request)
	context['filter_fields'] = [{'key': field['key'], 'label': field['label']} for field in SERVICE_REQUEST_FILTER_FIELDS]
	context['page_title'] = 'Service Request Drill-Through'
	context['row_limit'] = SERVICE_REQUEST_DRILL_PAGE_SIZE
	context.setdefault('error', '')
	context.setdefault('export_url', '')
	
	return render(request, 'dashboard/service_request_drillthrough.html', context)


@login_required
def service_request_drill_data(request):
	"""Return drill-down chart data for a specific category (JSON endpoint)."""
	drill_category = request.GET.get('drill_category', '').strip()
	if not drill_category:
		return JsonResponse({'error': 'No category specified'}, status=400)
	
	# Get base query info with all filters
	query_info = _prepare_service_request_drill_query(request)
	if query_info.get('error'):
		return JsonResponse({'error': 'Failed to prepare query'}, status=500)
	
	columns = query_info['columns']
	filter_clauses = list(query_info['filter_clauses'])
	filter_params = list(query_info['filter_params'])
	dedup_columns = query_info.get('dedup_columns', [])
	
	# Add category filter for drill-down
	category_col = _find_column_by_keywords(columns, ['category'])
	if category_col:
		filter_clauses.append(f"{_quote_ident(category_col)} ILIKE %s")
		filter_params.append(f"%{drill_category}%")
	
	# Build deduped base query with category filter
	distinct_column_idents = [_quote_ident(col) for col in dedup_columns if col]
	if distinct_column_idents:
		dedup_source_sql = f"SELECT DISTINCT {', '.join(distinct_column_idents)} FROM {SERVICE_REQUEST_DRILL_VIEW}"
	else:
		dedup_source_sql = f"SELECT DISTINCT * FROM {SERVICE_REQUEST_DRILL_VIEW}"
	if filter_clauses:
		dedup_source_sql += ' WHERE ' + ' AND '.join(filter_clauses)
	
	# Fetch drill-down chart data (item-level breakdown)
	chart_data = _fetch_service_request_drill_chart_data(columns, filter_clauses, filter_params, dedup_source_sql)
	
	return JsonResponse(chart_data)


def _fetch_service_request_drill_chart_data(columns, filter_clauses, filter_params, dedup_source_sql=None):
	"""Fetch item-level breakdown for drilled category."""
	chart_payload = {
		'category_breakdown': {'labels': [], 'values': []},
		'move_in_within_five_days': {'labels': [], 'values': []}
	}
	
	item_col = _find_column_by_keywords(columns, ['item'])
	created_col = _find_column_by_keywords(columns, ['created', 'date']) or _find_column_by_keywords(columns, ['created'])
	move_in_col = (
		_find_column_by_keywords(columns, ['move', 'in', 'date'])
		or _find_exact_column(columns, 'Move In Date')
		or _find_exact_column(columns, 'Move-In Date')
		or _find_exact_column(columns, 'Move-in Date')
		or _find_exact_column(columns, 'move_in_date')
		or _find_exact_column(columns, 'Move In Date Time')
		or _find_exact_column(columns, 'move_in_date_time')
		or _find_column_by_keywords(columns, ['move', 'in'])
	)
	
	if dedup_source_sql:
		base_sql = dedup_source_sql
	else:
		base_sql = f"SELECT DISTINCT * FROM {SERVICE_REQUEST_DRILL_VIEW}"
		if filter_clauses:
			base_sql += ' WHERE ' + ' AND '.join(filter_clauses)
	
	# Item breakdown for category chart
	if item_col:
		item_ident = _quote_ident(item_col)
		item_sql = (
			f"SELECT {item_ident} AS item_label, COUNT(*)::bigint AS total_count "
			f"FROM ({base_sql}) sr "
			f"WHERE {item_ident} IS NOT NULL AND TRIM({item_ident}::text) <> '' "
			f"GROUP BY {item_ident} "
			"ORDER BY total_count DESC LIMIT 12"
		)
		try:
			with connection.cursor() as cur:
				cur.execute(item_sql, filter_params)
				rows = cur.fetchall()
				for label, total in rows:
					chart_payload['category_breakdown']['labels'].append(str(label))
					chart_payload['category_breakdown']['values'].append(float(total or 0))
		except Exception as exc:
			print('Service request item drill chart query failed:', exc)
	
	# Move-in chart with item breakdown
	if item_col and created_col and move_in_col:
		item_ident = _quote_ident(item_col)
		created_ident = _quote_ident(created_col)
		move_in_ident = _quote_ident(move_in_col)
		move_sql = (
			f"SELECT {item_ident} AS item_label, COUNT(*)::bigint AS total_count "
			f"FROM ({base_sql}) sr "
			f"WHERE {item_ident} IS NOT NULL AND TRIM({item_ident}::text) <> '' "
			f"AND {created_ident} IS NOT NULL AND {move_in_ident} IS NOT NULL "
			f"AND ABS(({created_ident}::date - {move_in_ident}::date)) <= 5 "
			f"GROUP BY {item_ident} "
			"ORDER BY total_count DESC LIMIT 12"
		)
		try:
			with connection.cursor() as cur:
				cur.execute(move_sql, filter_params)
				rows = cur.fetchall()
				for label, total in rows:
					chart_payload['move_in_within_five_days']['labels'].append(str(label))
					chart_payload['move_in_within_five_days']['values'].append(float(total or 0))
		except Exception as exc:
			print('Service request move-in drill chart query failed:', exc)
	
	return chart_payload


@login_required
def service_request_drillthrough_export(request):
	"""CSV export for service request drill-through."""
	query_info = _prepare_service_request_drill_query(request)
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
	dedup_columns = query_info.get('dedup_columns', [])
	distinct_column_idents = [_quote_ident(col) for col in dedup_columns if col]
	if distinct_column_idents:
		dedup_source_sql = (
			f"SELECT DISTINCT {', '.join(distinct_column_idents)} FROM {SERVICE_REQUEST_DRILL_VIEW}"
		)
	else:
		dedup_source_sql = f"SELECT DISTINCT * FROM {SERVICE_REQUEST_DRILL_VIEW}"
	if filter_clauses:
		dedup_source_sql += ' WHERE ' + ' AND '.join(filter_clauses)

	order_alias = 'created_date' if 'created_date' in alias_order else (alias_order[0] if alias_order else None)
	data_sql = f"SELECT {', '.join(select_parts)} FROM ({dedup_source_sql}) distinct_rows"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} DESC NULLS LAST"

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, list(filter_params))
			rows = cur.fetchall()
	except Exception as exc:
		print('Service request drill-through export failed:', exc)
		return HttpResponse('Failed to export data.', status=500)

	timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
	filename = f"service_request_drillthrough_{timestamp}.csv"
	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = f'attachment; filename="{filename}"'

	writer = csv.writer(response)
	writer.writerow([col['label'] for col in display_columns])
	for raw in rows:
		row_dict = dict(zip(alias_order, raw))
		writer.writerow([_format_service_request_value(alias, row_dict.get(alias)) for alias in alias_order])

	return response


# ============================================================================
# AVERAGE TURN TIME DRILL-THROUGH VIEWS
# ============================================================================

AVG_TURN_TIME_DRILL_VIEW = 'web_ai.avg_turn_time_drill_through'
AVG_TURN_TIME_DRILL_PAGE_SIZE = 500

AVG_TURN_TIME_FILTER_FIELDS = [
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'keywords': ['regional', 'vp']},
	{'key': 'regional_manager', 'label': 'Regional Manager', 'keywords': ['regional', 'area', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
]

AVG_TURN_TIME_TABLE_FIELDS = [
	{'key': 'property_name', 'label': 'Property Name', 'keywords': ['property', 'name']},
	{'key': 'community', 'label': 'Community', 'keywords': ['community']},
	{'key': 'unit', 'label': 'Unit', 'exact': 'unit', 'keywords': ['unit']},
	{'key': 'floor_plan', 'label': 'Floor Plan', 'exact': 'Floor Plan'},
	{'key': 'previous_lease_move_out', 'label': 'Previous Move Out', 'exact': 'Previous Lease Move Out'},
	{'key': 'make_ready_date', 'label': 'Make Ready Date', 'exact': 'Make Ready Date'},
	{'key': 'turn_time_measure_second', 'label': 'Turn Time (Days)', 'keywords': ['turn', 'time', 'measure']},
	{'key': 'vacant_days', 'label': 'Vacant Days', 'keywords': ['vacant', 'days']},
	{'key': 'capx_expense', 'label': 'CapEx Expense', 'keywords': ['capx', 'expense']},
	{'key': 'rehab_expense', 'label': 'Rehab Expense', 'keywords': ['rehab', 'expense']},
	{'key': 'standard_expense', 'label': 'Standard Expense', 'keywords': ['standard', 'expense']},
	{'key': 'regional_vp', 'label': 'Regional VP | Sr. VP', 'exact': 'Regional VP  | Sr. VP'},
	{'key': 'regional_area_manager', 'label': 'Regional Manager', 'keywords': ['regional', 'area', 'manager']},
	{'key': 'investor', 'label': 'Investor', 'keywords': ['investor']},
]


def _get_avg_turn_time_drill_columns():
	"""Get all columns from the avg turn time drill-through materialized view."""
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
				['web_ai', 'avg_turn_time_drill_through']
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Avg turn time drill-through column introspection failed:', exc)

	if columns:
		return columns

	try:
		with connection.cursor() as cur:
			cur.execute(
				"""
				SELECT attname
				FROM pg_attribute
				WHERE attrelid = 'web_ai.avg_turn_time_drill_through'::regclass
				  AND attnum > 0
				  AND NOT attisdropped
				ORDER BY attnum
				"""
			)
			columns = [row[0] for row in cur.fetchall()]
	except Exception as exc:
		print('Avg turn time drill-through pg_attribute introspection failed:', exc)

	return columns


def _fetch_avg_turn_time_filter_options(columns):
	"""Fetch distinct values for filterable avg turn time columns."""
	filter_options = {}
	for field in AVG_TURN_TIME_FILTER_FIELDS:
		col = None
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		if not col and 'keywords' in field:
			col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			continue

		ident = _quote_ident(col)
		try:
			with connection.cursor() as cur:
				cur.execute(
					f"SELECT DISTINCT {ident} FROM {AVG_TURN_TIME_DRILL_VIEW} "
					f"WHERE {ident} IS NOT NULL AND {ident} != '' ORDER BY {ident} LIMIT 500"
				)
				filter_options[field['key']] = [row[0] for row in cur.fetchall() if row[0]]
		except Exception as exc:
			print(f'Avg turn time drill-through filter options for {field["key"]} failed:', exc)
			filter_options[field['key']] = []

	return filter_options


def _format_avg_turn_time_value(alias, val):
	"""Format a value for avg turn time table display."""
	if val is None:
		return ''
	# Format expenses as currency
	if 'expense' in alias.lower():
		try:
			return f'${float(val):,.2f}'
		except:
			return str(val)
	# Format dates
	if 'date' in alias.lower() or 'move' in alias.lower():
		if hasattr(val, 'strftime'):
			return val.strftime('%Y-%m-%d')
	return str(val)


def _fetch_avg_turn_time_summary(filter_clauses, filter_params, columns, dedup_source_sql=None):
	"""Calculate summary metrics for avg turn time drill-through."""
	summary = {
		'apartment_homes': 0,
		'total_turns': 0,
		'avg_turn_days': 0,
		'percent_under_7_days': 0,
		'total_expenses': 0,
		'avg_turn_cost': 0,
	}

	# Initialize parameters at function scope
	local_clauses = list(filter_clauses) if filter_clauses else []
	local_params = list(filter_params) if filter_params else []

	# Apartment homes count should be calculated across the entire materialized view
	# Build apartment_homes count using all active filters EXCEPT the date
	# range filter. We detect the Previous Lease Move Out column and exclude any
	# clauses that reference it so date-range filtering does not affect this
	# metric while other filter panel selections still apply.
	apartment_homes = 0
	try:
		# identify the previous lease move out column (used for date filters)
		move_out_col = _find_exact_column(columns, 'Previous Lease Move Out')
		q_move_out = _quote_ident(move_out_col) if move_out_col else None

		non_date_clauses = []
		non_date_params = []
		# iterate clauses and params in order; allocate params to their clause
		params_iter = iter(local_params)
		for clause in local_clauses:
			ph = clause.count('%s')
			chunk = []
			for _ in range(ph):
				try:
					chunk.append(next(params_iter))
				except StopIteration:
					break
			# skip clauses that reference the previous move-out date column
			if q_move_out and q_move_out in clause:
				continue
			non_date_clauses.append(clause)
			non_date_params.extend(chunk)

		# Prefer counting distinct OneSite identifier (OneSiteID-Property-Unit)
		# to match expected apartment/home counts. Fall back to `unit` if
		# OneSite id is unavailable.
		one_site_col = _find_exact_column(columns, 'OneSiteID-Property-Unit')
		unit_col_exact = _find_exact_column(columns, 'unit')
		unit_col_kw = _find_column_by_keywords(columns, ['unit'])
		unit_col = unit_col_exact or unit_col_kw
		prop_col = _find_exact_column(columns, 'property_name') or _find_column_by_keywords(columns, ['property', 'name'])
		investor_col = _find_column_by_keywords(columns, ['investor'])
		if one_site_col:
			apt_sql = f'SELECT COUNT(DISTINCT {_quote_ident(one_site_col)}) FROM {AVG_TURN_TIME_DRILL_VIEW}'
		elif unit_col:
			apt_sql = f'SELECT COUNT(DISTINCT {_quote_ident(unit_col)}) FROM {AVG_TURN_TIME_DRILL_VIEW}'
		else:
			apt_sql = f'SELECT COUNT(DISTINCT "OneSiteID-Property-Unit") FROM {AVG_TURN_TIME_DRILL_VIEW}'
		# Only apply non-date filters (e.g., investor). Do not enforce
		# property/unit NOT NULL filters so the count matches the DB query
		# that excludes only the investor (Livcor) rows.
		all_clauses = list(non_date_clauses) if non_date_clauses else []
		# Ensure investor rows matching 'livcor' are excluded (keep only investor filter)
		if investor_col:
			# use a normalized check to exclude any investor containing 'livcor'
			all_clauses.append(f"LOWER(TRIM({_quote_ident(investor_col)}::text)) NOT LIKE %s")
			non_date_params.append('%livcor%')

		if all_clauses:
			apt_sql += ' WHERE ' + ' AND '.join(all_clauses)
		with connection.cursor() as cur:
			cur.execute(apt_sql, non_date_params)
			row = cur.fetchone()
			apartment_homes = row[0] if row and row[0] is not None else 0
	except Exception as exc:
		print('Apartment homes count (non-date filters) failed:', exc)
		apartment_homes = 0

	# Build summary SQL
	turn_time_col = _find_column_by_keywords(columns, ['turn', 'time', 'measure'])
	capx_col = _find_column_by_keywords(columns, ['capx', 'expense'])
	rehab_col = _find_column_by_keywords(columns, ['rehab', 'expense'])
	standard_col = _find_column_by_keywords(columns, ['standard', 'expense'])
	property_col = _find_column_by_keywords(columns, ['property', 'name'])

	# Build list of columns required for deduplication to ensure totals use unique rows
	dedup_columns = []
	dedup_seen = set()
	for col in [turn_time_col, capx_col, rehab_col, standard_col, property_col]:
		if col and col not in dedup_seen:
			dedup_columns.append(col)
			dedup_seen.add(col)
	one_site_col = _find_exact_column(columns, 'OneSiteID-Property-Unit')
	if one_site_col and one_site_col not in dedup_seen:
		dedup_columns.append(one_site_col)
		dedup_seen.add(one_site_col)
	move_out_col = _find_exact_column(columns, 'Previous Lease Move Out')
	if move_out_col and move_out_col not in dedup_seen:
		dedup_columns.append(move_out_col)
		dedup_seen.add(move_out_col)

	sql_parts = [
		'COUNT(*) as total_turns',
		f'COUNT(DISTINCT {_quote_ident(property_col)}) as apartment_homes' if property_col else 'COUNT(DISTINCT 1) as apartment_homes'
	]

	if turn_time_col:
		sql_parts.append(f'AVG({_quote_ident(turn_time_col)}) as avg_turn')
		sql_parts.append(f'SUM(CASE WHEN {_quote_ident(turn_time_col)} < 7 THEN 1 ELSE 0 END) as under_7_days')

	if capx_col and rehab_col and standard_col:
		sql_parts.append(
			f'SUM(COALESCE({_quote_ident(capx_col)}, 0) + '
			f'COALESCE({_quote_ident(rehab_col)}, 0) + '
			f'COALESCE({_quote_ident(standard_col)}, 0)) as total_exp'
		)
		sql_parts.append(
			f'AVG(COALESCE({_quote_ident(capx_col)}, 0) + '
			f'COALESCE({_quote_ident(rehab_col)}, 0) + '
			f'COALESCE({_quote_ident(standard_col)}, 0)) as avg_cost'
		)

	if not sql_parts:
		return summary

	# Build source SQL with distinct rows to avoid duplicate inflation
	if dedup_source_sql:
		source_sql = dedup_source_sql
	else:
		if dedup_columns:
			column_sql = ', '.join(_quote_ident(col) for col in dedup_columns)
		else:
			column_sql = '*'
		source_sql = f"SELECT DISTINCT {column_sql} FROM {AVG_TURN_TIME_DRILL_VIEW}"
		if local_clauses:
			source_sql += ' WHERE ' + ' AND '.join(local_clauses)

	sql = f"SELECT {', '.join(sql_parts)} FROM ({source_sql}) dedup"

	try:
		with connection.cursor() as cur:
			cur.execute(sql, local_params)
			data = dict(zip([desc[0] for desc in cur.description], cur.fetchone() or []))
	except Exception as exc:
		print('Avg turn time drill-through summary calculation failed:', exc)
		return summary

	total_turns = data.get('total_turns') or 0
	under_7_days = data.get('under_7_days') or 0
	
	# Use the unfiltered apartment_homes if the direct COUNT succeeded; otherwise
	# fall back to the filtered count returned by the summary query.
	if apartment_homes and apartment_homes > 0:
		summary['apartment_homes'] = apartment_homes
	else:
		summary['apartment_homes'] = data.get('apartment_homes') or 0
	summary['total_turns'] = total_turns
	summary['avg_turn_days'] = round(data.get('avg_turn') or 0, 2)
	summary['percent_under_7_days'] = round((under_7_days / total_turns * 100) if total_turns > 0 else 0, 2)
	summary['total_expenses'] = round(data.get('total_exp') or 0, 2)
	summary['avg_turn_cost'] = round(data.get('avg_cost') or 0, 2)

	return summary


def _fetch_avg_turn_time_chart_data(columns, filter_clauses, filter_params, dedup_source_sql=None):
	"""Build chart payload for avg turn time drill-through charts."""
	chart_payload = {
		'turn_costs': {
			'labels': [],
			'datasets': []
		},
		'completion': {
			'labels': [],
			'percentages': [],
			'total_turns': []
		}
	}
	labels = []
	total_turns_series = []
	standard_series = []
	capx_series = []
	rehab_series = []
	percent_series = []

	move_out_col = _find_exact_column(columns, 'Previous Lease Move Out')
	standard_col = _find_column_by_keywords(columns, ['standard', 'expense'])
	capx_col = _find_column_by_keywords(columns, ['capx', 'expense'])
	rehab_col = _find_column_by_keywords(columns, ['rehab', 'expense'])
	turn_time_col = _find_column_by_keywords(columns, ['turn', 'time', 'measure'])

	if not (move_out_col and standard_col and capx_col and rehab_col and turn_time_col):
		return json.dumps(chart_payload), False

	date_ident = _quote_ident(move_out_col)
	base_sql = dedup_source_sql
	if not base_sql:
		base_sql = f"SELECT * FROM {AVG_TURN_TIME_DRILL_VIEW}"
		if filter_clauses:
			base_sql += ' WHERE ' + ' AND '.join(filter_clauses)

	month_expr = f"DATE_TRUNC('month', {date_ident})::date"
	standard_expr = _clean_numeric_expr(standard_col)
	capx_expr = _clean_numeric_expr(capx_col)
	rehab_expr = _clean_numeric_expr(rehab_col)
	turn_expr = _clean_numeric_expr(turn_time_col)

	subquery = (
		"SELECT "
		f"{month_expr} AS month_bucket, "
		f"{standard_expr} AS standard_val, "
		f"{capx_expr} AS capx_val, "
		f"{rehab_expr} AS rehab_val, "
		f"{turn_expr} AS turn_days "
		f"FROM ({base_sql}) filtered "
		f"WHERE {date_ident} IS NOT NULL"
	)

	chart_sql = (
		"SELECT month_bucket, "
		"COUNT(*) AS total_turns, "
		"SUM(COALESCE(standard_val, 0)) AS standard_expense, "
		"SUM(COALESCE(capx_val, 0)) AS capx_expense, "
		"SUM(COALESCE(rehab_val, 0)) AS rehab_expense, "
		"SUM(CASE WHEN turn_days < 7 THEN 1 ELSE 0 END) AS under_7_days "
		f"FROM ({subquery}) s "
		"GROUP BY month_bucket "
		"ORDER BY month_bucket DESC "
		"LIMIT 12"
	)

	try:
		with connection.cursor() as cur:
			cur.execute(chart_sql, filter_params)
			rows = list(cur.fetchall())
	except Exception as exc:
		print('Avg turn time chart query failed:', exc)
		rows = []

	rows = rows[::-1]
	for row in rows:
		month_bucket, total_turns, standard_expense, capx_expense, rehab_expense, under_7_days = row
		label = month_bucket.strftime('%b-%Y') if hasattr(month_bucket, 'strftime') else str(month_bucket)
		labels.append(label)
		total_turns_series.append(int(total_turns or 0))
		standard_series.append(float(standard_expense or 0))
		capx_series.append(float(capx_expense or 0))
		rehab_series.append(float(rehab_expense or 0))
		total_turns_val = float(total_turns or 0)
		under_7_val = float(under_7_days or 0)
		percent = (under_7_val / total_turns_val * 100) if total_turns_val else 0
		percent_series.append(round(percent, 2))

	chart_payload['turn_costs']['labels'] = labels
	chart_payload['completion']['labels'] = labels
	chart_payload['completion']['percentages'] = percent_series
	chart_payload['completion']['total_turns'] = total_turns_series

	if labels:
		chart_payload['turn_costs']['datasets'] = [
			{'label': 'Total Turns', 'data': total_turns_series, 'series_type': 'count'},
			{'label': 'Standard Expense', 'data': standard_series, 'series_type': 'currency'},
			{'label': 'CapEx Expense', 'data': capx_series, 'series_type': 'currency'},
			{'label': 'Rehab Expense', 'data': rehab_series, 'series_type': 'currency'},
		]

	return json.dumps(chart_payload), bool(labels)


def _prepare_avg_turn_time_drill_query(request):
	"""Prepare avg turn time drill-through query components."""
	columns = _get_avg_turn_time_drill_columns()
	if not columns:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {},
				'filter_options': {},
				'filters': {},
				'error': 'The avg turn time drill-through view is currently unavailable.',
			},
		}

	select_parts = []
	display_columns = []
	alias_order = []
	alias_to_source = {}

	for field in AVG_TURN_TIME_TABLE_FIELDS:
		col = None
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		if not col and 'keywords' in field:
			col = _find_column_by_keywords(columns, field['keywords'])
		if not col:
			continue
		alias = field['key']
		alias_to_source[alias] = col
		alias_order.append(alias)
		display_columns.append({'key': alias, 'label': field['label']})
		quoted_alias = _quote_ident(alias) if alias and alias[0].isdigit() else alias
		select_parts.append(f"{_quote_ident(col)} AS {quoted_alias}")

	filter_options = _fetch_avg_turn_time_filter_options(columns)

	if not select_parts:
		return {
			'error': True,
			'error_context': {
				'table_columns': [],
				'table_rows': [],
				'metrics': {},
				'filter_options': filter_options,
				'filters': {},
				'error': 'No recognizable columns were found for the avg turn time drill-through dataset.',
			},
		}

	# Determine columns used for deduplication to avoid duplicate rows in downstream queries
	move_out_col = _find_exact_column(columns, 'Previous Lease Move Out')
	turn_time_col = _find_column_by_keywords(columns, ['turn', 'time', 'measure'])
	capx_col = _find_column_by_keywords(columns, ['capx', 'expense'])
	rehab_col = _find_column_by_keywords(columns, ['rehab', 'expense'])
	standard_col = _find_column_by_keywords(columns, ['standard', 'expense'])
	one_site_col = _find_exact_column(columns, 'OneSiteID-Property-Unit')

	dedup_columns = []
	dedup_seen = set()
	for alias in alias_order:
		src = alias_to_source.get(alias)
		if src and src not in dedup_seen:
			dedup_columns.append(src)
			dedup_seen.add(src)

	for extra in [move_out_col, turn_time_col, capx_col, rehab_col, standard_col, one_site_col]:
		if extra and extra not in dedup_seen:
			dedup_columns.append(extra)
			dedup_seen.add(extra)

	filter_clauses = []
	filter_params = []
	active_filters = {}

	# Exclude Livcor properties
	investor_col = _find_column_by_keywords(columns, ['investor'])
	if investor_col:
		investor_ident = _quote_ident(investor_col)
		filter_clauses.append(f"({investor_ident} IS NULL OR ({investor_ident}::text NOT ILIKE %s AND {investor_ident}::text NOT ILIKE %s))")
		filter_params.extend(['%BLACKSTONE/LIVCOR%', '%LIVCOR%'])

	# Handle date range filters (Previous Lease Move Out)
	start_date = request.GET.get('start_date', '').strip()
	end_date = request.GET.get('end_date', '').strip()

	if move_out_col:
		column_ident = _quote_ident(move_out_col)
		# If no dates provided, default to current month's data up to latest available date
		if not start_date and not end_date:
			from datetime import date as _date
			today = _date.today()
			first_of_month = _date(today.year, today.month, 1)
			# Query the latest available date for this column within current month
			max_sql = f"SELECT MAX({column_ident}) FROM {AVG_TURN_TIME_DRILL_VIEW} WHERE {column_ident} >= %s"
			try:
				with connection.cursor() as cur:
					cur.execute(max_sql, [first_of_month])
					max_row = cur.fetchone()
					max_val = max_row[0] if max_row else None
			except Exception:
				max_val = None
			# Determine end_date: use max_val if present and not in the future, otherwise use today
			if max_val:
				try:
					max_date_only = max_val.date() if hasattr(max_val, 'date') else max_val
				except Exception:
					max_date_only = max_val
				if max_date_only and max_date_only <= today:
					end_date_eff = max_date_only
				else:
					end_date_eff = today
			else:
				end_date_eff = today
			# Format as ISO date strings
			start_date = first_of_month.isoformat()
			end_date = end_date_eff.isoformat()
			filter_clauses.append(f"{column_ident} >= %s")
			filter_params.append(start_date)
			active_filters['start_date'] = start_date
			filter_clauses.append(f"{column_ident} <= %s")
			filter_params.append(end_date)
			active_filters['end_date'] = end_date
		else:
			if start_date:
				filter_clauses.append(f"{column_ident} >= %s")
				filter_params.append(start_date)
				active_filters['start_date'] = start_date
			if end_date:
				filter_clauses.append(f"{column_ident} <= %s")
				filter_params.append(end_date)
				active_filters['end_date'] = end_date

	for field in AVG_TURN_TIME_FILTER_FIELDS:
		values = _extract_filter_list(request, request.GET, field['key'])
		clean = [v.strip() for v in values if v and v.strip() and v.strip().lower() != 'all'] if values else []
		active_filters[field['key']] = values[0] if values else ''
		if not clean:
			continue
		if 'exact' in field:
			col = _find_exact_column(columns, field['exact'])
		else:
			col = _find_column_by_keywords(columns, field.get('keywords', []))
		if not col:
			continue
		clause_parts = []
		for val in clean:
			clause_parts.append(f"{_quote_ident(col)} ILIKE %s")
			filter_params.append(f"%{val}%")
		if clause_parts:
			filter_clauses.append('(' + ' OR '.join(clause_parts) + ')')

	# Apply Livcor exclusion only when the user has not explicitly filtered by investor.
	# If the investor filter is set (e.g., 'Livcor'), do not exclude it.
	investor_col = _find_column_by_keywords(columns, ['investor'])
	if investor_col:
		inv_selected = active_filters.get('investor', '') or ''
		if not inv_selected or inv_selected.strip().lower() in ['', 'all']:
			investor_ident = _quote_ident(investor_col)
			filter_clauses.append(f"({investor_ident} IS NULL OR ({investor_ident}::text NOT ILIKE %s AND {investor_ident}::text NOT ILIKE %s))")
			filter_params.extend(['%BLACKSTONE/LIVCOR%', '%LIVCOR%'])

	return {
		'error': False,
		'columns': columns,
		'display_columns': display_columns,
		'alias_order': alias_order,
		'alias_to_source': alias_to_source,
		'dedup_columns': dedup_columns,
		'select_parts': select_parts,
		'filter_clauses': filter_clauses,
		'filter_params': filter_params,
		'active_filters': active_filters,
		'filter_options': filter_options,
	}


def _build_avg_turn_time_drill_context(request):
	"""Build complete context for avg turn time drill-through view."""
	query_info = _prepare_avg_turn_time_drill_query(request)
	if query_info.get('error'):
		return query_info['error_context']

	columns = query_info['columns']
	display_columns = query_info['display_columns']
	alias_order = query_info['alias_order']
	select_parts = query_info['select_parts']
	filter_clauses = query_info['filter_clauses']
	filter_params = query_info['filter_params']
	active_filters = query_info['active_filters']
	filter_options = query_info['filter_options']
	dedup_columns = query_info.get('dedup_columns', [])

	distinct_column_idents = [_quote_ident(col) for col in dedup_columns if col]
	if distinct_column_idents:
		dedup_source_sql = (
			f"SELECT DISTINCT {', '.join(distinct_column_idents)} FROM {AVG_TURN_TIME_DRILL_VIEW}"
		)
	else:
		dedup_source_sql = f"SELECT DISTINCT * FROM {AVG_TURN_TIME_DRILL_VIEW}"
	if filter_clauses:
		dedup_source_sql += ' WHERE ' + ' AND '.join(filter_clauses)

	summary = _fetch_avg_turn_time_summary(filter_clauses, filter_params, columns, dedup_source_sql)
	chart_data, chart_has_data = _fetch_avg_turn_time_chart_data(columns, filter_clauses, filter_params, dedup_source_sql)

	# Count total matching records for pagination
	count_sql = f"SELECT COUNT(*) FROM ({dedup_source_sql}) distinct_rows"

	try:
		with connection.cursor() as cur:
			cur.execute(count_sql, filter_params)
			total_records = cur.fetchone()[0] or 0
	except Exception as exc:
		print('Avg turn time drill-through count query failed:', exc)
		total_records = 0

	page_size = AVG_TURN_TIME_DRILL_PAGE_SIZE
	page_param = request.GET.get('page') if hasattr(request, 'GET') else None
	try:
		requested_page = int(page_param) if page_param else 1
	except Exception:
		requested_page = 1
	if requested_page < 1:
		requested_page = 1
	if total_records:
		total_pages = (total_records + page_size - 1) // page_size
		page = min(requested_page, total_pages)
	else:
		total_pages = 1
		page = 1
	offset = (page - 1) * page_size if total_records else 0

	order_alias = 'previous_lease_move_out' if 'previous_lease_move_out' in alias_order else ('make_ready_date' if 'make_ready_date' in alias_order else (alias_order[0] if alias_order else None))
	data_sql = f"SELECT {', '.join(select_parts)} FROM ({dedup_source_sql}) distinct_rows"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} DESC NULLS LAST"
	data_sql += " LIMIT %s OFFSET %s"
	data_params = list(filter_params) + [page_size, offset]

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, data_params)
			result = cur.fetchall()
	except Exception as exc:
		print('Avg turn time drill-through data query failed:', exc)
		result = []

	for raw in result:
		row_dict = dict(zip(alias_order, raw))
		ordered_values = [_format_avg_turn_time_value(alias, row_dict.get(alias)) for alias in alias_order]
		rows.append(ordered_values)

	start_index = offset + 1 if total_records and rows else 0
	end_index = offset + len(rows)
	if total_records and end_index > total_records:
		end_index = total_records

	base_query_pairs = []
	if hasattr(request.GET, 'lists'):
		for key, values in request.GET.lists():
			if key == 'page':
				continue
			for val in values:
				if val:
					base_query_pairs.append((key, val))
	
	dashboard_return_url = request.GET.get('return_url') or reverse('dashboard')
	if base_query_pairs:
		base_query_pairs.append(('return_url', dashboard_return_url))
	
	def build_page_url(pg):
		pairs = list(base_query_pairs)
		pairs.append(('page', pg))
		return f"{reverse('avg_turn_time_drillthrough')}?{urlencode(pairs, doseq=True)}"
	
	prev_url = build_page_url(page - 1) if page > 1 else None
	next_url = build_page_url(page + 1) if page < total_pages else None

	export_pairs = list(base_query_pairs)
	export_base = reverse('avg_turn_time_drillthrough_export')
	if export_pairs:
		export_url = f"{export_base}?{urlencode(export_pairs, doseq=True)}"
	else:
		export_url = export_base

	reset_base = reverse('avg_turn_time_drillthrough')
	if request.GET.get('return_url'):
		drill_reset_url = f"{reset_base}?{urlencode({'return_url': request.GET.get('return_url')})}"
	else:
		drill_reset_url = reset_base

	filter_blocks = []
	for field in AVG_TURN_TIME_FILTER_FIELDS:
		key = field['key']
		filter_blocks.append({
			'key': key,
			'label': field['label'],
			'options': filter_options.get(key, []),
			'selected': active_filters.get(key, ''),
		})

	return {
		'table_columns': display_columns,
		'table_rows': rows,
		'metrics': summary,
		'filters': {
			**active_filters,
			'start_date': active_filters.get('start_date', ''),
			'end_date': active_filters.get('end_date', ''),
		},
		'filter_options': filter_options,
		'filter_blocks': filter_blocks,
		'chart_data': chart_data,
		'chart_has_data': chart_has_data,
		'columns': columns,
		'pagination': {
			'page': page,
			'total_pages': total_pages,
			'total_count': total_records,
			'start_index': start_index,
			'end_index': end_index,
			'page_size': page_size,
			'has_prev': page > 1,
			'has_next': page < total_pages,
			'prev_url': prev_url,
			'next_url': next_url,
		},
		'dashboard_return_url': dashboard_return_url,
		'drill_reset_url': drill_reset_url,
		'export_url': export_url,
	}


@login_required
def avg_turn_time_drillthrough_view(request):
	"""View for avg turn time drill-through page."""
	context = _build_avg_turn_time_drill_context(request)
	return render(request, 'dashboard/avg_turn_time_drillthrough.html', context)


@login_required
def avg_turn_time_drillthrough_csv(request):
	"""Export avg turn time drill-through data as CSV."""
	query_info = _prepare_avg_turn_time_drill_query(request)
	if query_info.get('error'):
		return HttpResponse('Data unavailable', status=500)

	display_columns = query_info['display_columns']
	alias_order = query_info['alias_order']
	select_parts = query_info['select_parts']
	filter_clauses = query_info['filter_clauses']
	filter_params = query_info['filter_params']
	dedup_columns = query_info.get('dedup_columns', [])

	order_alias = 'make_ready_date' if 'make_ready_date' in alias_order else (alias_order[0] if alias_order else None)
	distinct_column_idents = [_quote_ident(col) for col in dedup_columns if col]
	if distinct_column_idents:
		dedup_source_sql = (
			f"SELECT DISTINCT {', '.join(distinct_column_idents)} FROM {AVG_TURN_TIME_DRILL_VIEW}"
		)
	else:
		dedup_source_sql = f"SELECT DISTINCT * FROM {AVG_TURN_TIME_DRILL_VIEW}"
	if filter_clauses:
		dedup_source_sql += ' WHERE ' + ' AND '.join(filter_clauses)

	data_sql = f"SELECT {', '.join(select_parts)} FROM ({dedup_source_sql}) distinct_rows"
	if order_alias:
		data_sql += f" ORDER BY {order_alias} DESC NULLS LAST"
	data_sql += " LIMIT 10000"

	rows = []
	try:
		with connection.cursor() as cur:
			cur.execute(data_sql, filter_params)
			result = cur.fetchall()
	except Exception as exc:
		print('Avg turn time drill-through CSV export failed:', exc)
		return HttpResponse('Export failed', status=500)

	for raw in result:
		row_dict = dict(zip(alias_order, raw))
		ordered_values = [_format_avg_turn_time_value(alias, row_dict.get(alias)) for alias in alias_order]
		rows.append(ordered_values)

	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = 'attachment; filename="avg_turn_time_data.csv"'

	writer = csv.writer(response)
	writer.writerow([col['label'] for col in display_columns])
	for row in rows:
		writer.writerow(row)

	return response


# ============================================================================
# Trade Out Drill-Through Views
# ============================================================================

def _format_trade_out_value(index, value):
	"""Format trade-out drill-through values for display.
	
	Args:
		index: Column index (0-11)
		value: Raw value from database
		
	Returns:
		Formatted string for display
	"""
	from datetime import datetime, date
	from decimal import Decimal
	
	# Column indices: 0=property_name, 1=unit, 2=floor_plan, 3=renewal_new_lease,
	# 4=current_lease_start_date, 5=trade_out_dollar, 6=current_lease_effective_rent,
	# 7=previous_lease_effective_rent, 8=current_lease_term, 9=previous_lease_term,
	# 10=community, 11=investor
	
	if value is None:
		return '-'
	
	# Date field (index 4)
	if index == 4:
		if isinstance(value, (datetime, date)):
			return value.strftime('%Y-%m-%d')
		try:
			parsed = datetime.strptime(str(value), '%Y-%m-%d').date()
			return parsed.strftime('%Y-%m-%d')
		except Exception:
			return str(value)
	
	# Currency fields (indices 5, 6, 7)
	if index in [5, 6, 7]:
		try:
			if isinstance(value, Decimal):
				value = float(value)
			num_val = float(value)
			return f'${num_val:,.2f}'
		except Exception:
			return str(value)
	
	# Text fields - just return as string
	text = str(value).strip()
	return text if text else '-'


@login_required
def trade_out_drillthrough_view(request):
	"""View for trade-out drill-through page showing renewal vs new lease trade-out analysis."""
	from django.db import connection
	from decimal import Decimal
	from datetime import date
	from dateutil.relativedelta import relativedelta
	
	# Get filter parameters
	community = request.GET.get('community', '')
	regional_vp = request.GET.get('regional_vp', '')
	regional_manager = request.GET.get('regional_manager', '')
	investor = request.GET.get('investor', '')
	start_date = request.GET.get('start_date', '')
	end_date = request.GET.get('end_date', '')
	criteria = request.GET.get('criteria', 'start')  # 'start' or 'signed'
	
	# Default to previous month if no dates provided
	if not start_date and not end_date:
		today = date.today()
		# Calculate first and last day of previous month
		first_of_current_month = date(today.year, today.month, 1)
		last_of_previous_month = first_of_current_month - relativedelta(days=1)
		first_of_previous_month = date(last_of_previous_month.year, last_of_previous_month.month, 1)
		start_date = first_of_previous_month.isoformat()
		end_date = last_of_previous_month.isoformat()
	
	# Build WHERE clause
	where_clauses = []
	params = []
	
	if community:
		where_clauses.append('community = %s')
		params.append(community)
	if regional_vp:
		where_clauses.append('"Regional VP  | Sr. VP" = %s')
		params.append(regional_vp)
	if regional_manager:
		where_clauses.append('regional_area_manager = %s')
		params.append(regional_manager)
	if investor:
		where_clauses.append('investor = %s')
		params.append(investor)
	
	# Apply date filter based on criteria
	# 'start' = Current Lease Start Date, 'signed' = Current_lease_App_Signed Date
	date_field = '"Current Lease Start Date"' if criteria == 'start' else '"Current_lease_App_Signed Date"'
	if start_date:
		where_clauses.append(f'{date_field} >= %s')
		params.append(start_date)
	if end_date:
		where_clauses.append(f'{date_field} <= %s')
		params.append(end_date)
	
	where_sql = ' WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''

	trade_out_chart = {'labels': [], 'values': [], 'iso_dates': []}

	# Calculate metrics using raw SQL
	with connection.cursor() as cursor:
		# Renewal metrics
		renewal_where = where_sql
		renewal_params = params.copy()
		if where_sql:
			renewal_where += ' AND "Renewal/New Lease" = %s'
		else:
			renewal_where = ' WHERE "Renewal/New Lease" = %s'
		renewal_params.append('Renewal')
		
		# Get renewal metrics (Trade Out % column is corrupted in the materialized view, skip it)
		cursor.execute(f'SELECT COUNT(*) FROM web_ai.trade_out_drill_through {renewal_where}', renewal_params)
		renewal_count = cursor.fetchone()[0] or 0

		cursor.execute(f'SELECT AVG("Trade Out $") FROM web_ai.trade_out_drill_through {renewal_where}', renewal_params)
		renewal_trade_out_avg_dollar = Decimal(str(cursor.fetchone()[0] or 0))

		# Compute Avg Trade Out % for Renewals using the DAX logic:
		# (SUM(CurrentLeaseEffRent for renewals with previous>0) - SUM(PreviousLeaseEffRent for same)) /
		# NULLIF(SUM(PreviousLeaseEffRent for same), 0)
		try:
			# Add condition for previous rent > 0 (cast money to numeric for comparison)
			renewal_sum_where = renewal_where + ' AND ("Previous Lease Effective Rent")::numeric > 0'
			cursor.execute(f'SELECT SUM(("Current Lease Effective Rent")::numeric), SUM(("Previous Lease Effective Rent")::numeric) FROM web_ai.trade_out_drill_through {renewal_sum_where}', renewal_params)
			_sums = cursor.fetchone()
			sum_cur = Decimal(str(_sums[0] or 0))
			sum_prev = Decimal(str(_sums[1] or 0))
			if sum_prev and sum_prev != 0:
				frac = (sum_cur - sum_prev) / sum_prev
				# round to 4 decimal places like DAX then store as fraction (not percent)
				from decimal import ROUND_HALF_UP
				renewal_trade_out_avg_percent = frac.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
			else:
				renewal_trade_out_avg_percent = Decimal('0')
		except Exception:
			# fallback to 0 on any error
			renewal_trade_out_avg_percent = Decimal('0')
		
		# New lease metrics
		new_lease_where = where_sql
		new_lease_params = params.copy()
		if where_sql:
			new_lease_where += ' AND "Renewal/New Lease" = %s'
		else:
			new_lease_where = ' WHERE "Renewal/New Lease" = %s'
		new_lease_params.append('New')
		
		# Get new lease metrics (Trade Out % column is corrupted in the materialized view, skip it)
		cursor.execute(f'SELECT COUNT(*) FROM web_ai.trade_out_drill_through {new_lease_where}', new_lease_params)
		new_lease_count = cursor.fetchone()[0] or 0
		
		# Compute Avg Trade Out $ for New leases using DAX logic:
		# avg(CASE WHEN Current_Lease_Rate_Type = 'New' AND Previous_Lease_Eff_Rent > 0 THEN trade_out_dollar END)
		try:
			cursor.execute(f'SELECT AVG((trade_out_dollar)::numeric) FROM web_ai.trade_out_drill_through {new_lease_where} AND ("Previous Lease Effective Rent")::numeric > 0', new_lease_params)
			new_lease_trade_out_avg_dollar = Decimal(str(cursor.fetchone()[0] or 0))
		except Exception:
			# fallback to previous simple average if something fails
			cursor.execute(f'SELECT AVG(("Trade Out $")::numeric) FROM web_ai.trade_out_drill_through {new_lease_where}', new_lease_params)
			new_lease_trade_out_avg_dollar = Decimal(str(cursor.fetchone()[0] or 0))
		
		# Compute Avg Trade Out % for New leases using the DAX logic (same as Renewals but for 'New')
		try:
			# Add condition for previous rent > 0 and cast money to numeric
			new_sum_where = new_lease_where + ' AND ("Previous Lease Effective Rent")::numeric > 0'
			cursor.execute(f'SELECT SUM(("Current Lease Effective Rent")::numeric), SUM(("Previous Lease Effective Rent")::numeric) FROM web_ai.trade_out_drill_through {new_sum_where}', new_lease_params)
			_nsum = cursor.fetchone()
			nsum_cur = Decimal(str(_nsum[0] or 0))
			nsum_prev = Decimal(str(_nsum[1] or 0))
			if nsum_prev and nsum_prev != 0:
				nfrac = (nsum_cur - nsum_prev) / nsum_prev
				from decimal import ROUND_HALF_UP
				new_lease_trade_out_avg_percent = nfrac.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
			else:
				new_lease_trade_out_avg_percent = Decimal('0')
		except Exception:
			new_lease_trade_out_avg_percent = Decimal('0')
		
		# Calculate renewal percentage
		total_leases = renewal_count + new_lease_count
		renewal_percentage = (renewal_count / total_leases * 100) if total_leases > 0 else 0
		
		# Get distinct values for dropdowns
		cursor.execute('SELECT DISTINCT community FROM web_ai.trade_out_drill_through WHERE community IS NOT NULL ORDER BY community')
		communities = [row[0] for row in cursor.fetchall()]
		
		cursor.execute('SELECT DISTINCT "Regional VP  | Sr. VP" FROM web_ai.trade_out_drill_through WHERE "Regional VP  | Sr. VP" IS NOT NULL ORDER BY "Regional VP  | Sr. VP"')
		regional_vps = [row[0] for row in cursor.fetchall()]
		
		cursor.execute('SELECT DISTINCT regional_area_manager FROM web_ai.trade_out_drill_through WHERE regional_area_manager IS NOT NULL ORDER BY regional_area_manager')
		regional_managers = [row[0] for row in cursor.fetchall()]
		
		cursor.execute('SELECT DISTINCT investor FROM web_ai.trade_out_drill_through WHERE investor IS NOT NULL ORDER BY investor')
		investors = [row[0] for row in cursor.fetchall()]
		
		# Get total count for pagination
		count_sql = f'SELECT COUNT(*) FROM web_ai.trade_out_drill_through{where_sql}'
		cursor.execute(count_sql, params)
		total_records = cursor.fetchone()[0]
		
		# Get paginated data
		page_size = 100
		page = int(request.GET.get('page', 1))
		offset = (page - 1) * page_size
		total_pages = (total_records + page_size - 1) // page_size
		
		data_sql = f'''
			SELECT 
				property_name, unit, "Floor Plan", "Renewal/New Lease",
				"Current Lease Start Date", "Trade Out $",
				"Current Lease Effective Rent", "Previous Lease Effective Rent",
				"Current Lease Term", "Previous Lease Term", community, investor
			FROM web_ai.trade_out_drill_through
			{where_sql}
			ORDER BY "Current Lease Start Date" DESC
			LIMIT %s OFFSET %s
		'''
		cursor.execute(data_sql, params + [page_size, offset])

		raw_rows = cursor.fetchall()

		# Build trade-out trend data grouped by selected date field
		chart_where_sql = where_sql
		chart_params = list(params)
		if chart_where_sql:
			chart_where_sql += f' AND {date_field} IS NOT NULL'
		else:
			chart_where_sql = f' WHERE {date_field} IS NOT NULL'
		chart_sql = f'''
			SELECT DATE_TRUNC('day', {date_field})::date AS chart_date,
				AVG(("Trade Out $")::numeric) AS avg_trade_out
			FROM web_ai.trade_out_drill_through
			{chart_where_sql}
			GROUP BY chart_date
			ORDER BY chart_date
		'''
		cursor.execute(chart_sql, chart_params)
		for chart_date, avg_trade_out in cursor.fetchall():
			if not chart_date:
				continue
			trade_out_chart['iso_dates'].append(chart_date.isoformat())
			trade_out_chart['labels'].append(chart_date.strftime('%b %d'))
			trade_out_chart['values'].append(float(avg_trade_out or 0))

		# Format each row for display
		table_rows = []
		for raw_row in raw_rows:
			formatted_row = [_format_trade_out_value(idx, val) for idx, val in enumerate(raw_row)]
			table_rows.append(formatted_row)
	
	# Define table columns for display
	table_columns = [
		{'label': 'Property Name'},
		{'label': 'Unit'},
		{'label': 'Floor Plan'},
		{'label': 'Renewal/New Lease'},
		{'label': 'Current Lease Start Date'},
		{'label': 'Trade Out $'},
		{'label': 'Current Lease Effective Rent'},
		{'label': 'Previous Lease Effective Rent'},
		{'label': 'Current Lease Term'},
		{'label': 'Previous Lease Term'},
		{'label': 'Community'},
		{'label': 'Investor'},
	]
	
	# Build pagination URLs
	base_query_pairs = []
	for key in ['community', 'regional_vp', 'regional_manager', 'investor', 'criteria', 'start_date', 'end_date', 'return_url']:
		value = request.GET.get(key)
		if value:
			base_query_pairs.append((key, value))
	
	base_drill_url = reverse('trade_out_drillthrough')
	
	def _build_page_url(target_page):
		pairs = list(base_query_pairs)
		if target_page > 1:
			pairs.append(('page', target_page))
		query = urlencode(pairs, doseq=True)
		return f"{base_drill_url}?{query}" if query else base_drill_url
	
	start_index = offset + 1 if total_records and table_rows else 0
	end_index = offset + len(table_rows)
	if total_records and end_index > total_records:
		end_index = total_records
	
	pagination = {
		'page': page,
		'page_size': page_size,
		'total_pages': total_pages,
		'has_prev': page > 1,
		'has_next': bool(total_records and page < total_pages),
		'prev_url': _build_page_url(page - 1) if page > 1 else '',
		'next_url': _build_page_url(page + 1) if total_records and page < total_pages else '',
		'start_index': start_index,
		'end_index': end_index,
		'total_count': total_records,
	}

	trade_out_axis_label = 'Current Lease Signed Date' if criteria == 'signed' else 'Current Lease Start Date'
	
	# Build export URL
	export_pairs = [(k, v) for (k, v) in base_query_pairs if k != 'return_url']
	export_base = reverse('trade_out_drillthrough_export')
	export_query = urlencode(export_pairs, doseq=True)
	export_url = f"{export_base}?{export_query}" if export_query else export_base
	
	context = {
		'page_title': 'Trade Out Drill-Through',
		'metrics': {
			'renewals': renewal_count,
			'renewal_percentage': renewal_percentage,
			'renewal_trade_out_avg_dollar': renewal_trade_out_avg_dollar,
			'renewal_trade_out_avg_percent': renewal_trade_out_avg_percent * 100,  # Convert to percentage
			'new_leases': new_lease_count,
			'new_lease_trade_out_avg_dollar': new_lease_trade_out_avg_dollar,
			'new_lease_trade_out_avg_percent': new_lease_trade_out_avg_percent * 100,  # Convert to percentage
		},
		'trade_out_chart': json.dumps(trade_out_chart),
		'trade_out_chart_has_data': bool(trade_out_chart['labels']),
		'trade_out_axis_label': trade_out_axis_label,
		'trade_out_chart_series_label': 'Trade Out $',
		'filters': {
			'community': community,
			'regional_vp': regional_vp,
			'regional_manager': regional_manager,
			'investor': investor,
			'start_date': start_date,
			'end_date': end_date,
			'criteria': criteria,
		},
		'communities': communities,
		'regional_vps': regional_vps,
		'regional_managers': regional_managers,
		'investors': investors,
		'table_rows': table_rows,
		'table_columns': table_columns,
		'pagination': pagination,
		'export_url': export_url,
		'dashboard_return_url': request.GET.get('return_url', reverse('dashboard')),
		'drill_reset_url': request.path,
		'filter_blocks': [
			{
				'key': 'community',
				'label': 'Community',
				'options': communities,
				'selected': community,
			},
			{
				'key': 'regional_vp',
				'label': 'Regional VP',
				'options': regional_vps,
				'selected': regional_vp,
			},
			{
				'key': 'regional_manager',
				'label': 'Regional Manager',
				'options': regional_managers,
				'selected': regional_manager,
			},
			{
				'key': 'investor',
				'label': 'Investor',
				'options': investors,
				'selected': investor,
			},
		],
	}
	
	return render(request, 'dashboard/trade_out_drillthrough.html', context)


@login_required
def trade_out_drillthrough_export(request):
	"""CSV export for trade-out drill-through."""
	from django.db import connection
	import csv
	from django.utils import timezone
	
	# Get filter parameters
	community = request.GET.get('community', '')
	regional_vp = request.GET.get('regional_vp', '')
	regional_manager = request.GET.get('regional_manager', '')
	investor = request.GET.get('investor', '')
	start_date = request.GET.get('start_date', '')
	end_date = request.GET.get('end_date', '')

	# Use same criteria semantics as the drill view ('start' or 'signed')
	# default to 'start' so the date field maps to Current Lease Start Date
	criteria = request.GET.get('criteria', 'start')
	# Default to previous month if no dates provided (match drill view behaviour)
	if not start_date and not end_date:
		from datetime import date
		from dateutil.relativedelta import relativedelta
		today = date.today()
		first_of_current_month = date(today.year, today.month, 1)
		last_of_previous_month = first_of_current_month - relativedelta(days=1)
		first_of_previous_month = date(last_of_previous_month.year, last_of_previous_month.month, 1)
		start_date = first_of_previous_month.isoformat()
		end_date = last_of_previous_month.isoformat()

	# Build WHERE clause
	where_clauses = []
	params = []
	
	if community:
		where_clauses.append('community = %s')
		params.append(community)
	if regional_vp:
		where_clauses.append('"Regional VP  | Sr. VP" = %s')
		params.append(regional_vp)
	if regional_manager:
		where_clauses.append('regional_area_manager = %s')
		params.append(regional_manager)
	if investor:
		where_clauses.append('investor = %s')
		params.append(investor)

	# Map criteria values to the same date field names used by the view
	# 'start' -> Current Lease Start Date, 'signed' -> Current_lease_App_Signed Date
	date_field = '"Current Lease Start Date"' if criteria == 'start' else '"Current_lease_App_Signed Date"'
	if start_date:
		where_clauses.append(f'{date_field} >= %s')
		params.append(start_date)
	if end_date:
		where_clauses.append(f'{date_field} <= %s')
		params.append(end_date)
	
	where_sql = ' WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''
	
	# Get data using raw SQL (excluding Trade Out % due to database corruption)
	with connection.cursor() as cursor:
		data_sql = f'''
			SELECT 
				property_name, unit, "Floor Plan", "Renewal/New Lease",
				"Current Lease Start Date", "Current Lease End Date",
				"Trade Out $",
				"Current Lease Effective Rent", "Previous Lease Effective Rent",
				"Current Lease Term", "Previous Lease Term",
				"Current Lease Type", community, "Regional VP  | Sr. VP",
				regional_area_manager, investor
			FROM web_ai.trade_out_drill_through
			{where_sql}
			ORDER BY "Current Lease Start Date" DESC
			LIMIT 10000
		'''
		cursor.execute(data_sql, params)
		records = cursor.fetchall()
	
	# Create CSV response
	timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
	filename = f"trade_out_drillthrough_{timestamp}.csv"
	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = f'attachment; filename="{filename}"'
	
	writer = csv.writer(response)
	writer.writerow([
		'Property Name', 'Unit', 'Floor Plan', 'Renewal/New Lease',
		'Current Lease Start Date', 'Current Lease End Date',
		'Trade Out $',
		'Current Lease Effective Rent', 'Previous Lease Effective Rent',
		'Current Lease Term', 'Previous Lease Term',
		'Current Lease Type', 'Community', 'Regional VP',
		'Regional Manager', 'Investor'
	])
	
	for record in records:
		writer.writerow(record)
	
	return response


def _calculate_avg_turn_time_from_drill_through(request, params, selected_months):
	"""
	Calculate avg_turn_time KPI from unit-level drill-through data instead of
	property-level aggregates. This fixes data quality issues in olympus_lease_kpis_trend_monthly
	where average_turn_time values are incorrect.
	
	Args:
		request: HttpRequest object
		params: Query parameters dict
		selected_months: List of selected month start dates
	
	Returns:
		float: Rounded average turn time in days, or None if no data
	"""
	from django.db import connection
	from datetime import datetime, timedelta
	
	# Extract filters
	inv_list = request.GET.getlist('investor') if request.GET.getlist('investor') else ([params.get('investor')] if params.get('investor') else [])
	regional_list = request.GET.getlist('regional_manager') if request.GET.getlist('regional_manager') else ([params.get('regional_manager')] if params.get('regional_manager') else [])
	community_list = request.GET.getlist('community') if request.GET.getlist('community') else ([params.get('community')] if params.get('community') else [])
	
	# Build WHERE clauses
	where_clauses = []
	sql_params = []
	
	# Date filter - use "Previous Lease Move Out" column which represents when the unit became vacant
	if selected_months:
		# Build date ranges for all selected months
		month_ranges = []
		for month_start in selected_months:
			if isinstance(month_start, str):
				month_start = datetime.strptime(month_start, '%Y-%m-%d').date()
			
			# Calculate last day of month
			if month_start.month == 12:
				next_month = month_start.replace(year=month_start.year + 1, month=1, day=1)
			else:
				next_month = month_start.replace(month=month_start.month + 1, day=1)
			
			month_ranges.append((month_start, next_month))
		
		# Create OR condition for all month ranges
		if len(month_ranges) == 1:
			where_clauses.append('"Previous Lease Move Out" >= %s AND "Previous Lease Move Out" < %s')
			sql_params.extend([month_ranges[0][0], month_ranges[0][1]])
		else:
			month_conditions = []
			for start, end in month_ranges:
				month_conditions.append('("Previous Lease Move Out" >= %s AND "Previous Lease Move Out" < %s)')
				sql_params.extend([start, end])
			where_clauses.append(f'({" OR ".join(month_conditions)})')
	
	# Apply filters
	if inv_list and inv_list[0]:
		placeholders = ','.join(['%s'] * len(inv_list))
		where_clauses.append(f'investor IN ({placeholders})')
		sql_params.extend(inv_list)
	
	if regional_list and regional_list[0]:
		placeholders = ','.join(['%s'] * len(regional_list))
		where_clauses.append(f'regional_area_manager IN ({placeholders})')
		sql_params.extend(regional_list)
	
	if community_list and community_list[0]:
		placeholders = ','.join(['%s'] * len(community_list))
		where_clauses.append(f'property_name IN ({placeholders})')
		sql_params.extend(community_list)
	
	where_sql = ' WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''
	
	# Query drill-through table for average turn time
	with connection.cursor() as cursor:
		sql = f'''
			SELECT 
				AVG(turn_time_measure_second) as avg_turn_time,
				COUNT(*) as unit_count
			FROM web_ai.avg_turn_time_drill_through
			{where_sql}
			AND turn_time_measure_second IS NOT NULL
		'''
		cursor.execute(sql, sql_params)
		row = cursor.fetchone()
		
		if row and row[0] is not None:
			avg_turn = float(row[0])
			unit_count = row[1]
			# Log for debugging
			print(f"[AVG_TURN_TIME] Calculated from drill-through: {avg_turn:.2f} days ({unit_count} units)")
			return round(avg_turn, 1)
	
	return None


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
	finance_kpi_display = (
		format_finance_kpi_values(finance_kpi_raw, request=request, params=params, svc_ctx=svc_ctx)
		if finance_kpi_raw
		else format_finance_kpi_values({}, request=request, params=params, svc_ctx=svc_ctx)
	)

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
		
		# Calculate avg_turn_time from OlympusLeaseKpisTrendMonthly aggregated data
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

	# Build occupancy drill-through URL with same filter/period context
	occupancy_drill_url = reverse('occupancy_drillthrough')
	if drill_pairs:
		context['occupancy_drill_url'] = f"{occupancy_drill_url}?{urlencode(drill_pairs, doseq=True)}"
	else:
		context['occupancy_drill_url'] = occupancy_drill_url

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


# ─────────────────────────────────────────────────────────────────────────────────
# OCCUPANCY EOM DRILL-THROUGH
# ─────────────────────────────────────────────────────────────────────────────────

def occupancy_eom_drillthrough_view(request):
	"""
	Occupancy End-of-Month drill-through view.
	Displays property-level occupancy % in a pivot table: properties as rows, months as columns.
	Data source: OlympusLeaseTrendAnalysis.percentage_occupacy
	"""
	# Get filter parameters
	community = request.GET.get('community', '')
	regional_vp = request.GET.get('regional_vp', '')
	regional_manager = request.GET.get('regional_manager', '')
	investor = request.GET.get('investor', '')
	
	# Get period mode (monthly, quarterly, yearly)
	period_mode = request.GET.get('period_mode', 'monthly')
	
	# Build base query
	qs = OlympusLeaseTrendAnalysis.objects.all()
	
	# Apply filters
	if community and community.lower() not in ['', 'all']:
		qs = qs.filter(property_name=community)
	if regional_vp and regional_vp.lower() not in ['', 'all']:
		qs = qs.filter(regional_director=regional_vp)
	if regional_manager and regional_manager.lower() not in ['', 'all']:
		qs = qs.filter(regional_area_manager=regional_manager)
	if investor and investor.lower() not in ['', 'all']:
		qs = qs.filter(investor=investor)
	
	# Fetch all records with required fields
	records = qs.values(
		'property_name',
		'property_number',
		'snapshotdate',
		'percentage_occupacy',
		'investor',
		'regional_director',
		'regional_area_manager'
	).order_by('property_name', 'snapshotdate')
	
	# Build property × period pivot table
	property_data = {}  # {property_name: {period_key: occupancy_value}}
	periods = set()  # All unique periods
	
	for record in records:
		prop_name = record['property_name'] or 'Unknown'
		snapshot_date = record['snapshotdate']
		occupancy = record['percentage_occupacy']
		
		if not snapshot_date or occupancy is None:
			continue
		
		# Generate period key based on period_mode
		if period_mode == 'yearly':
			period_key = snapshot_date.strftime('%Y')
		elif period_mode == 'quarterly':
			quarter = (snapshot_date.month - 1) // 3 + 1
			period_key = f"Q{quarter}-{snapshot_date.year}"
		else:  # monthly (default)
			period_key = snapshot_date.strftime('%b-%Y')
		
		periods.add(period_key)
		
		# Initialize property dict if not exists
		if prop_name not in property_data:
			property_data[prop_name] = {
				'property_name': prop_name,
				'investor': record['investor'],
				'regional_vp': record['regional_director'],
				'regional_manager': record['regional_area_manager'],
				'periods': {}
			}
		
		# Store occupancy value for this property-period combination
		# If multiple snapshots exist for same period, take the latest (or average)
		if period_key not in property_data[prop_name]['periods']:
			property_data[prop_name]['periods'][period_key] = []
		property_data[prop_name]['periods'][period_key].append(occupancy)
	
	# Sort periods chronologically
	if period_mode == 'yearly':
		sorted_periods = sorted(periods, key=lambda x: int(x))
	elif period_mode == 'quarterly':
		sorted_periods = sorted(periods, key=lambda x: (int(x.split('-')[1]), int(x[1])))
	else:  # monthly
		sorted_periods = sorted(periods, key=lambda x: datetime.strptime(x, '%b-%Y'))
	
	# Calculate average occupancy for each property-period cell
	for prop_name in property_data:
		for period_key in property_data[prop_name]['periods']:
			values = property_data[prop_name]['periods'][period_key]
			avg_occupancy = round(sum(values) / len(values), 2) if values else 0.0
			property_data[prop_name]['periods'][period_key] = avg_occupancy
	
	# Convert to list for template
	property_rows = []
	for prop_name, data in sorted(property_data.items()):
		row = {
			'property_name': prop_name,
			'investor': data['investor'],
			'regional_vp': data['regional_vp'],
			'regional_manager': data['regional_manager'],
			'period_values': [data['periods'].get(p, None) for p in sorted_periods]
		}
		property_rows.append(row)
	
	# Fetch filter choices for dropdown population
	investors = OlympusLeaseTrendAnalysis.objects.values_list('investor', flat=True).distinct().order_by('investor')
	communities = OlympusLeaseTrendAnalysis.objects.values_list('property_name', flat=True).distinct().order_by('property_name')
	regional_vps = OlympusLeaseTrendAnalysis.objects.values_list('regional_director', flat=True).distinct().order_by('regional_director')
	regional_managers = OlympusLeaseTrendAnalysis.objects.values_list('regional_area_manager', flat=True).distinct().order_by('regional_area_manager')
	
	# Filter out None/empty values
	investors = [i for i in investors if i]
	communities = [c for c in communities if c]
	regional_vps = [r for r in regional_vps if r]
	regional_managers = [r for r in regional_managers if r]
	
	context = {
		'property_rows': property_rows,
		'period_columns': sorted_periods,
		'period_mode': period_mode,
		'selected_community': community,
		'selected_regional_vp': regional_vp,
		'selected_regional_manager': regional_manager,
		'selected_investor': investor,
		'investors': investors,
		'communities': communities,
		'regional_vps': regional_vps,
		'regional_managers': regional_managers,
		'total_properties': len(property_rows),
		'total_periods': len(sorted_periods),
		'dashboard_return_url': reverse('dashboard')
	}
	
	return render(request, 'dashboard/occupancy_eom_drillthrough.html', context)


def occupancy_eom_drillthrough_csv(request):
	"""CSV export for occupancy EOM drill-through data."""
	import csv
	
	# Reuse the same query logic as the main view
	# Get filter parameters
	community = request.GET.get('community', '')
	regional_vp = request.GET.get('regional_vp', '')
	regional_manager = request.GET.get('regional_manager', '')
	investor = request.GET.get('investor', '')
	period_mode = request.GET.get('period_mode', 'monthly')
	
	# Build base query
	qs = OlympusLeaseTrendAnalysis.objects.all()
	
	# Apply filters
	if community and community.lower() not in ['', 'all']:
		qs = qs.filter(property_name=community)
	if regional_vp and regional_vp.lower() not in ['', 'all']:
		qs = qs.filter(regional_director=regional_vp)
	if regional_manager and regional_manager.lower() not in ['', 'all']:
		qs = qs.filter(regional_area_manager=regional_manager)
	if investor and investor.lower() not in ['', 'all']:
		qs = qs.filter(investor=investor)
	
	# Fetch all records
	records = qs.values(
		'property_name',
		'property_number',
		'snapshotdate',
		'percentage_occupacy',
		'investor',
		'regional_director',
		'regional_area_manager'
	).order_by('property_name', 'snapshotdate')
	
	# Build pivot table
	property_data = {}
	periods = set()
	
	for record in records:
		prop_name = record['property_name'] or 'Unknown'
		snapshot_date = record['snapshotdate']
		occupancy = record['percentage_occupacy']
		
		if not snapshot_date or occupancy is None:
			continue
		
		if period_mode == 'yearly':
			period_key = snapshot_date.strftime('%Y')
		elif period_mode == 'quarterly':
			quarter = (snapshot_date.month - 1) // 3 + 1
			period_key = f"Q{quarter}-{snapshot_date.year}"
		else:
			period_key = snapshot_date.strftime('%b-%Y')
		
		periods.add(period_key)
		
		if prop_name not in property_data:
			property_data[prop_name] = {
				'property_name': prop_name,
				'investor': record['investor'],
				'regional_vp': record['regional_director'],
				'regional_manager': record['regional_area_manager'],
				'periods': {}
			}
		
		if period_key not in property_data[prop_name]['periods']:
			property_data[prop_name]['periods'][period_key] = []
		property_data[prop_name]['periods'][period_key].append(occupancy)
	
	# Sort periods
	if period_mode == 'yearly':
		sorted_periods = sorted(periods, key=lambda x: int(x))
	elif period_mode == 'quarterly':
		sorted_periods = sorted(periods, key=lambda x: (int(x.split('-')[1]), int(x[1])))
	else:
		sorted_periods = sorted(periods, key=lambda x: datetime.strptime(x, '%b-%Y'))
	
	# Calculate averages
	for prop_name in property_data:
		for period_key in property_data[prop_name]['periods']:
			values = property_data[prop_name]['periods'][period_key]
			avg_occupancy = round(sum(values) / len(values), 2) if values else 0.0
			property_data[prop_name]['periods'][period_key] = avg_occupancy
	
	# Create CSV response
	response = HttpResponse(content_type='text/csv')
	response['Content-Disposition'] = f'attachment; filename="occupancy_eom_{period_mode}.csv"'
	
	writer = csv.writer(response)
	
	# Write header
	header = ['Property Name', 'Investor', 'Regional VP', 'Regional Manager'] + sorted_periods
	writer.writerow(header)
	
	# Write data rows
	for prop_name, data in sorted(property_data.items()):
		row = [
			prop_name,
			data['investor'] or '',
			data['regional_vp'] or '',
			data['regional_manager'] or ''
		]
		row.extend([data['periods'].get(p, '') for p in sorted_periods])
		writer.writerow(row)
	
	return response


@login_required
def demographics_analytics(request):
	"""Demographics analytics page with professional insights.
	
	Analyzes resident demographic data from web_ai.demographics materialized view
	including age distribution, gender, employment, income levels, and residency patterns.
	"""
	from dashboard.models import Demographics
	from django.db.models import Avg, Count, Q, Max, Min, Sum, Case, When, IntegerField
	import json
	
	# Get filter parameters
	property_filter = request.GET.getlist('property') if request.GET.getlist('property') else None
	status_filter = request.GET.get('status', 'all')  # all, current, former
	
	# Base queryset
	queryset = Demographics.objects.all()
	
	# Apply property filter
	if property_filter:
		queryset = queryset.filter(property_name__in=property_filter)
	
	# Apply status filter
	if status_filter == 'current':
		queryset = queryset.filter(lease_level_occupancy_status__icontains='Current')
	elif status_filter == 'former':
		queryset = queryset.filter(lease_level_occupancy_status__icontains='Former')
	
	# Get distinct properties for filter dropdown
	all_properties = Demographics.objects.values_list('property_name', flat=True).distinct().order_by('property_name')
	
	# Calculate key metrics
	total_residents = queryset.count()
	
	# Age statistics (age is now IntegerField)
	age_stats = queryset.filter(age__isnull=False).aggregate(
		avg_age=Avg('age'),
		min_age=Min('age'),
		max_age=Max('age')
	)
	
	# Income statistics (now DecimalField - can aggregate directly)
	income_stats = queryset.filter(
		current_employment_estimated_annual_income__isnull=False
	).aggregate(
		avg_income=Avg('current_employment_estimated_annual_income'),
		min_income=Min('current_employment_estimated_annual_income'),
		max_income=Max('current_employment_estimated_annual_income'),
		total_estimated_income=Sum('current_employment_estimated_annual_income')
	)
	
	# Gender distribution
	gender_dist = queryset.filter(
		gender__isnull=False
	).exclude(gender='').values('gender').annotate(
		count=Count('site_id_property_unit_number')
	).order_by('-count')
	
	# Age distribution (buckets) - age is now integer
	age_buckets = {}
	try:
		qs_with_age = queryset.filter(age__isnull=False)
		age_buckets = {
			'18-25': qs_with_age.filter(age__gte=18, age__lte=25).count(),
			'26-35': qs_with_age.filter(age__gte=26, age__lte=35).count(),
			'36-45': qs_with_age.filter(age__gte=36, age__lte=45).count(),
			'46-55': qs_with_age.filter(age__gte=46, age__lte=55).count(),
			'56-65': qs_with_age.filter(age__gte=56, age__lte=65).count(),
			'66+': qs_with_age.filter(age__gte=66).count(),
		}
	except Exception:
		age_buckets = {'18-25': 0, '26-35': 0, '36-45': 0, '46-55': 0, '56-65': 0, '66+': 0}
	
	# Employment status
	employment_data = {
		'employed': queryset.filter(current_employment_name__isnull=False).exclude(current_employment_name='').count(),
		'income_reported': queryset.filter(current_employment_estimated_annual_income__isnull=False).count(),
	}
	
	# Marital status distribution
	marital_dist = queryset.filter(
		marital_status__isnull=False
	).exclude(marital_status='').values('marital_status').annotate(
		count=Count('site_id_property_unit_number')
	).order_by('-count')
	
	# Ethnicity distribution
	ethnicity_dist = queryset.filter(
		ethnicity__isnull=False
	).exclude(ethnicity='').values('ethnicity').annotate(
		count=Count('site_id_property_unit_number')
	).order_by('-count')
	
	# Income ranges (income is now DecimalField)
	income_ranges = {}
	try:
		qs_with_income = queryset.filter(current_employment_estimated_annual_income__isnull=False)
		income_ranges = {
			'<30k': qs_with_income.filter(current_employment_estimated_annual_income__lt=30000).count(),
			'30k-50k': qs_with_income.filter(current_employment_estimated_annual_income__gte=30000, current_employment_estimated_annual_income__lt=50000).count(),
			'50k-75k': qs_with_income.filter(current_employment_estimated_annual_income__gte=50000, current_employment_estimated_annual_income__lt=75000).count(),
			'75k-100k': qs_with_income.filter(current_employment_estimated_annual_income__gte=75000, current_employment_estimated_annual_income__lt=100000).count(),
			'100k+': qs_with_income.filter(current_employment_estimated_annual_income__gte=100000).count(),
		}
	except Exception:
		income_ranges = {'<30k': 0, '30k-50k': 0, '50k-75k': 0, '75k-100k': 0, '100k+': 0}
	
	# Lease signer vs occupant
	resident_types = {
		'lease_signers': queryset.filter(lease_signer='Yes').count(),
		'occupants': queryset.filter(occupant='Yes').count(),
		'co_signers': queryset.filter(co_signer='Yes').count(),
		'guarantors': queryset.filter(guarantor='Yes').count(),
	}
	
	# Screening history
	screening_stats = {
		'criminal_history': queryset.filter(criminal_history='Yes').count(),
		'evicted': queryset.filter(has_been_evicted='Yes').count(),
		'sued_rent': queryset.filter(has_been_sued_for_rent='Yes').count(),
		'broken_lease': queryset.filter(has_broken_lease='Yes').count(),
	}
	
	# Top properties by resident count
	top_properties = queryset.values('property_name').annotate(
		resident_count=Count('site_id_property_unit_number')
	).order_by('-resident_count')[:10]
	
	# Average ledger balance (now DecimalField)
	avg_balance = queryset.filter(
		ledger_balance__isnull=False
	).aggregate(
		avg_balance=Avg('ledger_balance')
	)
	
	# Prepare chart data
	age_chart_data = {
		'labels': list(age_buckets.keys()),
		'datasets': [{
			'label': 'Residents by Age Group',
			'data': list(age_buckets.values()),
			'backgroundColor': '#59E6F6',
			'borderColor': '#0E555A',
			'borderWidth': 2
		}]
	}
	
	gender_chart_data = {
		'labels': [g['gender'] for g in gender_dist],
		'datasets': [{
			'label': 'Gender Distribution',
			'data': [g['count'] for g in gender_dist],
			'backgroundColor': ['#59E6F6', '#C69A58', '#0E555A', '#95A3B3'],
			'borderWidth': 2
		}]
	}
	
	income_chart_data = {
		'labels': list(income_ranges.keys()),
		'datasets': [{
			'label': 'Income Distribution',
			'data': list(income_ranges.values()),
			'backgroundColor': '#C69A58',
			'borderColor': '#0E555A',
			'borderWidth': 2
		}]
	}
	
	# Prepare chart data
	age_chart_data = {
		'labels': list(age_buckets.keys()),
		'datasets': [{
			'label': 'Residents by Age Group',
			'data': list(age_buckets.values()),
			'backgroundColor': '#59E6F6',
			'borderColor': '#0E555A',
			'borderWidth': 2
		}]
	}
	
	gender_chart_data = {
		'labels': [g['gender'] for g in gender_dist],
		'datasets': [{
			'label': 'Gender Distribution',
			'data': [g['count'] for g in gender_dist],
			'backgroundColor': ['#59E6F6', '#C69A58', '#0E555A', '#95A3B3'],
			'borderWidth': 2
		}]
	}
	
	income_chart_data = {
		'labels': list(income_ranges.keys()),
		'datasets': [{
			'label': 'Income Distribution',
			'data': list(income_ranges.values()),
			'backgroundColor': '#C69A58',
			'borderColor': '#0E555A',
			'borderWidth': 2
		}]
	}
	
	# Get resident data for table (limit to 100 records for performance)
	resident_data = queryset.select_related().only(
		'property_name', 'first_name', 'last_name', 'age', 'gender',
		'marital_status', 'ethnicity', 'current_employment_name',
		'current_employment_estimated_annual_income', 'lease_level_occupancy_status',
		'lease_start_date', 'lease_end_date', 'ledger_balance',
		'lease_signer', 'occupant'
	)[:100]
	
	context = {
		'total_residents': total_residents,
		'age_stats': age_stats,
		'income_stats': income_stats,
		'gender_dist': gender_dist,
		'marital_dist': marital_dist,
		'ethnicity_dist': ethnicity_dist,
		'employment_data': employment_data,
		'resident_types': resident_types,
		'screening_stats': screening_stats,
		'top_properties': top_properties,
		'avg_balance': avg_balance,
		'age_chart_json': json.dumps(age_chart_data),
		'gender_chart_json': json.dumps(gender_chart_data),
		'income_chart_json': json.dumps(income_chart_data),
		'all_properties': all_properties,
		'selected_properties': property_filter or [],
		'status_filter': status_filter,
		'resident_data': resident_data,
	}
	
	return render(request, 'dashboard/demographics.html', context)


@login_required
def demographics_export_csv(request):
	"""Export filtered demographics data to CSV."""
	from dashboard.models import Demographics
	import csv
	from django.http import HttpResponse
	from datetime import datetime
	
	# Get filter parameters (same as main view)
	property_filter = request.GET.getlist('property') if request.GET.getlist('property') else None
	status_filter = request.GET.get('status', 'all')
	
	# Base queryset
	queryset = Demographics.objects.all()
	
	# Apply property filter
	if property_filter:
		queryset = queryset.filter(property_name__in=property_filter)
	
	# Apply status filter
	if status_filter == 'current':
		queryset = queryset.filter(lease_level_occupancy_status__icontains='Current')
	elif status_filter == 'former':
		queryset = queryset.filter(lease_level_occupancy_status__icontains='Former')
	
	# Create CSV response
	response = HttpResponse(content_type='text/csv')
	timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
	filename = f'demographics_export_{timestamp}.csv'
	response['Content-Disposition'] = f'attachment; filename="{filename}"'
	
	writer = csv.writer(response)
	
	# Write header
	writer.writerow([
		'Property Name',
		'Site ID',
		'Unit Number',
		'First Name',
		'Last Name',
		'Age',
		'Gender',
		'Marital Status',
		'Ethnicity',
		'Employment Name',
		'Annual Income',
		'Lease Signer',
		'Occupant',
		'Lease Start Date',
		'Lease End Date',
		'Occupancy Status',
		'Ledger Balance',
		'Criminal History',
		'Evicted',
		'Sued for Rent',
		'Broken Lease'
	])
	
	# Write data rows
	for record in queryset.select_related():
		writer.writerow([
			record.property_name or '',
			record.site_id_property_unit_number or '',
			record.unit_number or '',
			record.first_name or '',
			record.last_name or '',
			record.age or '',
			record.gender or '',
			record.marital_status or '',
			record.ethnicity or '',
			record.current_employment_name or '',
			record.current_employment_estimated_annual_income or '',
			record.lease_signer or '',
			record.occupant or '',
			record.lease_start_date or '',
			record.lease_end_date or '',
			record.lease_level_occupancy_status or '',
			record.ledger_balance or '',
			record.criminal_history or '',
			record.has_been_evicted or '',
			record.has_been_sued_for_rent or '',
			record.has_broken_lease or ''
		])
	
	return response


def sso_login(request):
	"""
	SSO login placeholder view.
	Redirect to Azure AD or other SSO provider login.
	"""
	from django.contrib import messages
	from django.shortcuts import redirect
	
	# For now, redirect back to login with a message
	# In production, this would redirect to Azure AD OAuth2 endpoint
	messages.info(request, 'SSO login is not yet configured. Please use username/password.')
	return redirect('login')
