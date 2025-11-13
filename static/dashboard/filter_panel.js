// Filter Panel JavaScript - Reusable filter panel logic
// This file provides all functionality for the Olympus filter panel component
// Usage: Include this file after your page content and ensure filter_action_url variable is set

(function(){
    const filterToggle = document.getElementById('filterToggle');
    const filterPane = document.getElementById('filterPane');
    const filterClose = document.getElementById('filterClose');
    const applyBtn = document.getElementById('applyFilters');
    const clearBtn = document.getElementById('clearFilters');

    function setFilter(open){
        if(!filterPane) return;
        filterPane.classList.toggle('open', open);
        filterPane.setAttribute('aria-hidden', !open);
        document.documentElement.classList.toggle('filter-open', open);
        // Also mark body or wrapper element if logic relies on it
        const mainEl = document.querySelector('.main');
        if(mainEl) mainEl.classList.toggle('filter-open', open);
        const chartsContainer = document.querySelector('.charts-container');
        if(chartsContainer) chartsContainer.classList.toggle('filter-open', open);
        const propertyTableParent = document.querySelector('div[style*="width:100%"]');
        if(propertyTableParent) propertyTableParent.classList.toggle('filter-open', open);
        // For financial reporting page
        const financialContainer = document.querySelector('.financial-container');
        if(financialContainer) financialContainer.classList.toggle('filter-open', open);
        // For analytics page
        const analyticsContainer = document.querySelector('.analytics-container');
        if(analyticsContainer) analyticsContainer.classList.toggle('filter-open', open);
    }

    if(filterToggle){ filterToggle.addEventListener('click', e=>{ e.stopPropagation(); setFilter(!filterPane.classList.contains('open')); }); }
    if(filterClose){ filterClose.addEventListener('click', e=>{ e.stopPropagation(); setFilter(false); }); }

    // Close on Escape
    document.addEventListener('keydown', function(e){ if(e.key==='Escape'){ if(filterPane && filterPane.classList.contains('open')) setFilter(false) } });

    // Multi-select chips rendering and removal
    function renderChips(selectEl, chipsContainerId){
        const container = document.getElementById(chipsContainerId);
        if(!container || !selectEl) return;
        container.innerHTML = ''; // Clear existing chips to prevent duplicates
        const selected = Array.from(selectEl.selectedOptions);
        selected.forEach(opt => {
            const chip = document.createElement('div');
            chip.className = 'filter-chip';
            chip.innerHTML = `<span>${opt.textContent}</span><button type="button" class="filter-chip-remove" data-value="${opt.value}" aria-label="Remove ${opt.textContent}">×</button>`;
            container.appendChild(chip);
            const removeBtn = chip.querySelector('.filter-chip-remove');
            removeBtn.addEventListener('click', function(ev){
                ev.stopPropagation();
                ev.preventDefault();
                opt.selected = false;
                
                // Also uncheck the corresponding checkbox in custom dropdown
                const checkbox = document.querySelector(`input[type="checkbox"][value="${CSS.escape(opt.value)}"]`);
                if(checkbox) checkbox.checked = false;
                
                // Update the dropdown toggle text
                const dropdownMap = {
                    'yearChips': 'yearDropdownToggle',
                    'quarterChips': 'quarterDropdownToggle',
                    'monthChips': 'monthDropdownToggle',
                    'investorChips': 'investorDropdownToggle',
                    'regionalChips': 'regionalDropdownToggle',
                    'communityChips': 'communityDropdownToggle'
                };
                
                const toggleId = dropdownMap[chipsContainerId];
                if(toggleId) {
                    const toggle = document.getElementById(toggleId);
                    if(toggle) {
                        const selectedOptions = Array.from(selectEl.selectedOptions);
                        if (selectedOptions.length === 0) {
                            const placeholderText = chipsContainerId.includes('year') ? 'Select years...' :
                                                   chipsContainerId.includes('quarter') ? 'Select quarters...' :
                                                   chipsContainerId.includes('month') ? 'Select months...' :
                                                   chipsContainerId.includes('investor') ? 'Select investors...' :
                                                   chipsContainerId.includes('regional') ? 'Select regional managers...' :
                                                   chipsContainerId.includes('community') ? 'Select communities...' :
                                                   'Select...';
                            toggle.innerHTML = `<span class="placeholder">${placeholderText}</span>`;
                        } else if (selectedOptions.length === 1) {
                            toggle.innerHTML = `<span class="selected-count">${selectedOptions[0].textContent}</span>`;
                        } else {
                            toggle.innerHTML = `<span class="selected-count">${selectedOptions.length} selected</span>`;
                        }
                    }
                }
                
                selectEl.dispatchEvent(new Event('change'));
            });
        });
    }

    // Custom dropdown with checkboxes functionality
    function initCustomDropdown(toggleId, menuId, selectId, chipsId, searchInputId) {
        const toggle = document.getElementById(toggleId);
        const menu = document.getElementById(menuId);
        const select = document.getElementById(selectId);
        const searchInput = document.getElementById(searchInputId);
        
        if (!toggle || !menu || !select) return;
        
        // Toggle dropdown
        toggle.addEventListener('click', function(e) {
            e.stopPropagation();
            const isOpen = menu.classList.contains('open');
            
            // Close all other dropdowns
            document.querySelectorAll('.dropdown-menu.open').forEach(m => {
                if (m !== menu) {
                    m.classList.remove('open');
                    m.previousElementSibling.classList.remove('open');
                }
            });
            
            menu.classList.toggle('open');
            toggle.classList.toggle('open');
            
            if (!isOpen && searchInput) {
                setTimeout(() => searchInput.focus(), 100);
            }
        });
        
        // Handle checkbox changes
        const checkboxes = menu.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(checkbox => {
            checkbox.addEventListener('change', function() {
                const option = select.querySelector(`option[value="${this.value}"]`);
                if (option) {
                    option.selected = this.checked;
                }
                
                updateToggleText();
                renderChips(select, chipsId);
            });
            
            const label = checkbox.nextElementSibling;
            if (label) {
                label.addEventListener('click', function(e) {
                    e.preventDefault();
                    checkbox.checked = !checkbox.checked;
                    checkbox.dispatchEvent(new Event('change'));
                });
            }
        });
        
        // Search functionality
        if (searchInput) {
            searchInput.addEventListener('input', function() {
                const searchTerm = this.value.toLowerCase();
                const items = menu.querySelectorAll('.dropdown-menu-item');
                
                items.forEach(item => {
                    const label = item.querySelector('label');
                    if (label) {
                        const text = label.textContent.toLowerCase();
                        item.style.display = text.includes(searchTerm) ? 'flex' : 'none';
                    }
                });
            });
            
            searchInput.addEventListener('click', function(e) {
                e.stopPropagation();
            });
        }
        
        // Update toggle button text based on selections
        function updateToggleText() {
            const selectedOptions = Array.from(select.selectedOptions);
            const placeholder = toggle.querySelector('.placeholder');
            
            if (selectedOptions.length === 0) {
                toggle.innerHTML = `<span class="placeholder">${placeholder ? placeholder.textContent : 'Select...'}</span>`;
                toggle.innerHTML += '<span></span>';
            } else if (selectedOptions.length === 1) {
                toggle.innerHTML = `<span class="selected-count">${selectedOptions[0].textContent}</span>`;
                toggle.innerHTML += '<span></span>';
            } else {
                toggle.innerHTML = `<span class="selected-count">${selectedOptions.length} selected</span>`;
                toggle.innerHTML += '<span></span>';
            }
        }
        
        // Initialize toggle text
        updateToggleText();
        
        // Sync checkboxes with select element (for pre-selected values)
        Array.from(select.selectedOptions).forEach(option => {
            const checkbox = menu.querySelector(`input[value="${option.value}"]`);
            if (checkbox) {
                checkbox.checked = true;
            }
        });
        
        // Update chips on change
        select.addEventListener('change', function() {
            updateToggleText();
            renderChips(select, chipsId);
        });
        
        return { toggle, menu, select, updateToggleText };
    }

    // Initialize custom dropdowns for period filters
    const yearDropdown = initCustomDropdown('yearDropdownToggle', 'yearDropdownMenu', 'filterPeriodYear', 'yearChips', 'yearSearchInput');
    const quarterDropdown = initCustomDropdown('quarterDropdownToggle', 'quarterDropdownMenu', 'filterPeriodQuarter', 'quarterChips', 'quarterSearchInput');
    const monthDropdown = initCustomDropdown('monthDropdownToggle', 'monthDropdownMenu', 'filterPeriodMonth', 'monthChips', 'monthSearchInput');
    
    // Initialize custom dropdowns for other filters
    const investorDropdown = initCustomDropdown('investorDropdownToggle', 'investorDropdownMenu', 'filterInvestor', 'investorChips', 'investorSearchInput');
    const regionalDropdown = initCustomDropdown('regionalDropdownToggle', 'regionalDropdownMenu', 'filterRegional', 'regionalChips', 'regionalSearchInput');
    const communityDropdown = initCustomDropdown('communityDropdownToggle', 'communityDropdownMenu', 'filterCommunity', 'communityChips', 'communitySearchInput');

    // Add Select All / Clear All functionality
    document.querySelectorAll('.select-all-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const dropdown = this.dataset.dropdown;
            const menu = document.getElementById(`${dropdown}DropdownMenu`);
            
            let select;
            if (dropdown === 'year' || dropdown === 'quarter' || dropdown === 'month') {
                select = document.getElementById(`filterPeriod${dropdown.charAt(0).toUpperCase() + dropdown.slice(1)}`);
            } else if (dropdown === 'investor') {
                select = document.getElementById('filterInvestor');
            } else if (dropdown === 'regional') {
                select = document.getElementById('filterRegional');
            } else if (dropdown === 'community') {
                select = document.getElementById('filterCommunity');
            }
            
            if (menu && select) {
                const checkboxes = menu.querySelectorAll('.dropdown-menu-item:not([style*="display: none"]) input[type="checkbox"]');
                checkboxes.forEach(cb => {
                    cb.checked = true;
                    const option = select.querySelector(`option[value="${cb.value}"]`);
                    if (option) option.selected = true;
                });
                
                const dropdowns = { 
                    year: yearDropdown, 
                    quarter: quarterDropdown, 
                    month: monthDropdown,
                    investor: investorDropdown,
                    regional: regionalDropdown,
                    community: communityDropdown
                };
                if (dropdowns[dropdown]) dropdowns[dropdown].updateToggleText();
                renderChips(select, `${dropdown}Chips`);
            }
        });
    });

    document.querySelectorAll('.clear-all-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const dropdown = this.dataset.dropdown;
            const menu = document.getElementById(`${dropdown}DropdownMenu`);
            
            let select;
            if (dropdown === 'year' || dropdown === 'quarter' || dropdown === 'month') {
                select = document.getElementById(`filterPeriod${dropdown.charAt(0).toUpperCase() + dropdown.slice(1)}`);
            } else if (dropdown === 'investor') {
                select = document.getElementById('filterInvestor');
            } else if (dropdown === 'regional') {
                select = document.getElementById('filterRegional');
            } else if (dropdown === 'community') {
                select = document.getElementById('filterCommunity');
            }
            
            if (menu && select) {
                const checkboxes = menu.querySelectorAll('input[type="checkbox"]');
                checkboxes.forEach(cb => {
                    cb.checked = false;
                    const option = select.querySelector(`option[value="${cb.value}"]`);
                    if (option) option.selected = false;
                });
                
                const dropdowns = { 
                    year: yearDropdown, 
                    quarter: quarterDropdown, 
                    month: monthDropdown,
                    investor: investorDropdown,
                    regional: regionalDropdown,
                    community: communityDropdown
                };
                if (dropdowns[dropdown]) dropdowns[dropdown].updateToggleText();
                renderChips(select, `${dropdown}Chips`);
            }
        });
    });

    // Close dropdowns when clicking outside
    document.addEventListener('click', function(e) {
        if (!e.target.closest('.custom-dropdown')) {
            document.querySelectorAll('.dropdown-menu.open').forEach(menu => {
                menu.classList.remove('open');
                const toggle = menu.previousElementSibling;
                if (toggle) toggle.classList.remove('open');
            });
        }
    });

    // Period mode toggle logic (Year / Quarter / Month)
    const periodModeBtns = document.querySelectorAll('.period-mode-btn');
    const yearDropdownContainer = document.getElementById('yearDropdownContainer');
    const quarterDropdownContainer = document.getElementById('quarterDropdownContainer');
    const monthDropdownContainer = document.getElementById('monthDropdownContainer');
    const yearChips = document.getElementById('yearChips');
    const quarterChips = document.getElementById('quarterChips');
    const monthChips = document.getElementById('monthChips');
    let activeMode = 'year';

    function setMode(mode){
        activeMode = mode;
        periodModeBtns.forEach(b=>{ b.style.background = (b.dataset.mode === mode) ? '#eef6fb' : 'transparent'; b.setAttribute('aria-pressed', b.dataset.mode === mode); });
        
        // Close all dropdowns when switching modes
        document.querySelectorAll('.dropdown-menu.open').forEach(menu => {
            menu.classList.remove('open');
            const toggle = menu.previousElementSibling;
            if (toggle) toggle.classList.remove('open');
        });
        
        // Show/hide appropriate dropdowns and their chips
        if(yearDropdownContainer) yearDropdownContainer.style.display = (mode === 'year') ? 'block' : 'none';
        if(yearChips) yearChips.style.display = (mode === 'year') ? 'flex' : 'none';
        
        if(quarterDropdownContainer) quarterDropdownContainer.style.display = (mode === 'quarter') ? 'block' : 'none';
        if(quarterChips) quarterChips.style.display = (mode === 'quarter') ? 'flex' : 'none';
        
        if(monthDropdownContainer) monthDropdownContainer.style.display = (mode === 'month') ? 'block' : 'none';
        if(monthChips) monthChips.style.display = (mode === 'month') ? 'flex' : 'none';
    }
    periodModeBtns.forEach(b=>b.addEventListener('click',()=>setMode(b.dataset.mode)));
    
    // Shared function for Apply filter logic
    function applyFilters() {
        const loadingOverlay = document.getElementById('loadingOverlay');
        if(loadingOverlay) loadingOverlay.style.display = 'flex';
        
        const params = new URLSearchParams(window.location.search);
        
        const periodYearSel = document.getElementById('filterPeriodYear');
        const periodYears = periodYearSel ? Array.from(periodYearSel.selectedOptions).map(opt => opt.value) : [];
        
        const periodQuarterSel = document.getElementById('filterPeriodQuarter');
        const periodQuarters = periodQuarterSel ? Array.from(periodQuarterSel.selectedOptions).map(opt => opt.value) : [];
        
        const periodMonthSel = document.getElementById('filterPeriodMonth');
        const periodMonths = periodMonthSel ? Array.from(periodMonthSel.selectedOptions).map(opt => opt.value) : [];
        
        const investorSel = document.getElementById('filterInvestor');
        const investors = investorSel ? Array.from(investorSel.selectedOptions).map(opt => opt.value) : [];
        
        const regionalSel = document.getElementById('filterRegional');
        const regionals = regionalSel ? Array.from(regionalSel.selectedOptions).map(opt => opt.value) : [];
        
        const communitySel = document.getElementById('filterCommunity');
        const communities = communitySel ? Array.from(communitySel.selectedOptions).map(opt => opt.value) : [];
        
        // Clear old filters
        params.delete('period'); params.delete('period_mode'); params.delete('period_year'); params.delete('period_quarter'); params.delete('period_month'); params.delete('investor'); params.delete('regional_manager'); params.delete('community');
        
        // Set new filters
        let mode = activeMode;
        if(mode) params.set('period_mode', mode);
        
        if(mode === 'year') {
            periodYears.forEach(py => { if(py) params.append('period_year', py); });
            if(periodYears.length > 0) params.set('period', periodYears[0]);
        }
        if(mode === 'quarter') {
            periodQuarters.forEach(pq => { if(pq) params.append('period_quarter', pq); });
            if(periodQuarters.length > 0) params.set('period', periodQuarters[0]);
        }
        if(mode === 'month') {
            periodMonths.forEach(pm => { if(pm) params.append('period_month', pm); });
            if(periodMonths.length > 0) params.set('period', periodMonths[0]);
        }
        
        investors.forEach(inv => { if(inv) params.append('investor', inv); });
        regionals.forEach(rm => { if(rm) params.append('regional_manager', rm); });
        communities.forEach(comm => { if(comm) params.append('community', comm); });
        
        // Get the filter action URL from a global variable or default to current page
        const actionUrl = window.filterActionUrl || window.location.pathname;
        
        // Check if we're on analytics page (actionUrl contains '/analytics/query/')
        // Analytics page needs POST, other pages use GET redirect
        if (actionUrl.includes('/analytics/query/')) {
            // For analytics, call the global analyze function if available
            if (window.triggerAnalytics && typeof window.triggerAnalytics === 'function') {
                // Close the filter panel
                setFilter(false);
                // Call the analytics function
                window.triggerAnalytics();
                // Hide loading overlay since analytics will show its own
                if(loadingOverlay) loadingOverlay.style.display = 'none';
            } else {
                // Fallback: Try to submit the form directly
                const analyticsForm = document.getElementById('analyticsForm');
                if (analyticsForm) {
                    setFilter(false);
                    analyticsForm.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
                    if(loadingOverlay) loadingOverlay.style.display = 'none';
                } else {
                    // Last resort: show error
                    alert('Analytics form not found. Please use the Analyze Data button.');
                    if(loadingOverlay) loadingOverlay.style.display = 'none';
                }
            }
        } else {
            // For dashboard and other pages, use GET redirect
            window.location.href = actionUrl + '?' + params.toString();
        }
    }

    // Shared function for Clear filter logic
    function clearFilters() {
        const loadingOverlay = document.getElementById('loadingOverlay');
        if(loadingOverlay) loadingOverlay.style.display = 'flex';
        
        const params = new URLSearchParams(window.location.search);
        params.delete('period');
        params.delete('period_mode');
        params.delete('period_year');
        params.delete('period_quarter');
        params.delete('period_month');
        params.delete('investor');
        params.delete('regional_manager');
        params.delete('community');
        
        const actionUrl = window.filterActionUrl || window.location.pathname;
        window.location.href = actionUrl + '?' + params.toString();
    }

    // Bind Apply/Clear buttons
    if(applyBtn){ applyBtn.addEventListener('click', applyFilters); }
    if(clearBtn){ clearBtn.addEventListener('click', clearFilters); }
    
    const applyBtnTop = document.getElementById('applyFiltersTop');
    if(applyBtnTop){ applyBtnTop.addEventListener('click', applyFilters); }
    
    const clearBtnTop = document.getElementById('clearFiltersTop');
    if(clearBtnTop){ clearBtnTop.addEventListener('click', clearFilters); }

    // Clicking outside filter pane should close it
    document.addEventListener('click', function(e){
        if(!filterPane) return;
        if(filterPane.classList.contains('open')){
            if(e.target && (e.target.classList && (e.target.classList.contains('filter-chip-remove') || e.target.closest('.filter-chip')))){
                return;
            }
            const isClickInsidePane = filterPane.contains(e.target);
            const isClickOnToggle = filterToggle && filterToggle.contains(e.target);
            if(!isClickInsidePane && !isClickOnToggle){
                setFilter(false);
            }
        }
    });

    // Expose functions globally for external use
    window.OlympusFilterPanel = {
        setMode: setMode,
        applyFilters: applyFilters,
        clearFilters: clearFilters,
        renderChips: renderChips
    };
})();
