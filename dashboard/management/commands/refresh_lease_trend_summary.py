from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.core.cache import cache
import time


CREATE_MV_SQL = """
CREATE MATERIALIZED VIEW IF NOT EXISTS web_ai.lease_trend_summary AS
WITH latest_per_prop_year AS (
	SELECT * FROM (
		SELECT *, ROW_NUMBER() OVER (PARTITION BY property_number, EXTRACT(YEAR FROM snapshot_date) ORDER BY snapshot_date DESC) AS rn
		FROM web_ai.olympus_lease_trend_analysis
		WHERE snapshot_date IS NOT NULL
	) t WHERE rn = 1
)
SELECT
	property_number,
	property_name,
	(EXTRACT(YEAR FROM snapshot_date))::int AS year,
	max(snapshot_date) AS snapshotdate,
	AVG(effective_rent)::numeric(12,2) AS avg_effective_rent,
	AVG(market_rent)::numeric(12,2) AS avg_market_rent,
	SUM(occupied_units)::int AS sum_occupied_units,
	SUM(total_units)::int AS sum_total_units,
	AVG(percentage_occupacy)::numeric(6,2) AS avg_occupancy,
	investor,
	regional_area_manager
FROM latest_per_prop_year
GROUP BY property_number, property_name, (EXTRACT(YEAR FROM snapshot_date)), investor, regional_area_manager;
"""

CREATE_INDEX_SQL = [
	"CREATE INDEX IF NOT EXISTS idx_lease_trend_summary_year ON web_ai.lease_trend_summary (year);",
	"CREATE INDEX IF NOT EXISTS idx_lease_trend_summary_investor ON web_ai.lease_trend_summary (investor);",
	"CREATE INDEX IF NOT EXISTS idx_lease_trend_summary_region ON web_ai.lease_trend_summary (regional_area_manager);",
	"CREATE INDEX IF NOT EXISTS idx_lease_trend_summary_property ON web_ai.lease_trend_summary (property_number);",
]


class Command(BaseCommand):
	help = 'Create or refresh the lease_trend_summary materialized view in web_ai and warm cache.'

	def handle(self, *args, **options):
		self.stdout.write('Starting refresh_lease_trend_summary...')
		start = time.time()
		cur = connection.cursor()

		try:
			# Create or replace materialized view (idempotent)
			self.stdout.write('Creating/replacing materialized view...')
			cur.execute('BEGIN;')
			try:
				# CREATE MATERIALIZED VIEW IF NOT EXISTS is supported in recent PG; use CREATE OR REPLACE for idempotence
				cur.execute(CREATE_MV_SQL)
				cur.execute('COMMIT;')
			except Exception:
				# If the session is in failed state, rollback and re-execute without transaction wrapper
				cur.execute('ROLLBACK;')
				cur.execute(CREATE_MV_SQL)

			# Refresh materialized view concurrently if possible
			self.stdout.write('Refreshing materialized view (CONCURRENTLY where supported)...')
			try:
				cur.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY web_ai.lease_trend_summary;')
			except Exception:
				# Fallback to non-concurrent refresh if CONCURRENTLY fails (permissions or MV not set up)
				cur.execute('REFRESH MATERIALIZED VIEW web_ai.lease_trend_summary;')

			# Create indexes if missing
			self.stdout.write('Ensuring indexes exist...')
			for sql in CREATE_INDEX_SQL:
				try:
					cur.execute(sql)
				except Exception:
					# ignore errors from index creation
					pass

			# Warm a small set of caches used by dashboard (kpi and charts for 'all' period)
			self.stdout.write('Warming dashboard caches...')
			# We will compute a minimal KPI and year-series snapshot and cache them for 1 hour
			from dashboard.services import DashboardService
			svc = DashboardService({'period_mode': 'year', 'period_year': 'all'})
			ctx = svc.get_context()
			cache.set('dashboard:summary:period=all', ctx, timeout=60 * 60)

			self.stdout.write(self.style.SUCCESS('refresh_lease_trend_summary completed in %.2fs' % (time.time() - start)))

		except Exception as e:
			cur.execute('ROLLBACK;')
			import traceback; traceback.print_exc()
			self.stdout.write(self.style.ERROR('Error during refresh_lease_trend_summary: %s' % str(e)))
