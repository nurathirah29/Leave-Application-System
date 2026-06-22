// Shared Helpers 
function showInvalid(input) { input.classList.add("is-invalid"); }
function clearInvalid(input) { input.classList.remove("is-invalid"); }
function isWorkingDay(date) { return date.getDay() !== 0 && date.getDay() !== 6; }
function calculateWorkingDays(startDate, endDate, publicHolidays = []) {
    let count = 0, current = new Date(startDate);
    while (current <= endDate) {
        const dateStr = current.toISOString().split('T')[0];
        const isHoliday = publicHolidays.some(h => h.date === dateStr);
        if (isWorkingDay(current) && !isHoliday) count++;
        current.setDate(current.getDate() + 1);
    }
    return count;
}

// Helper function to show error message below input
function showErrorMessage(errorElementId, message) {
    const errorElement = document.getElementById(errorElementId);
    if (errorElement) {
        errorElement.innerHTML = message;
        errorElement.style.display = 'block';
    }
}

// Hide error message
function hideErrorMessage(errorElementId) {
    const errorElement = document.getElementById(errorElementId);
    if (errorElement) {
        errorElement.textContent = '';
        errorElement.style.display = 'none';
    }
}

function initLeaveForm(config) {
    const {
        formId,
        leaveReasonId,
        otherReasonFieldId,
        otherReasonInputId,
        startDateId,
        endDateId,
        noDaysId,
        remarkId,
        startRemarkId,
        endRemarkId,
        endRemarkFieldId,
        remarkLabelId = "remarkLabel",
        startRemarkLabelId = "startRemarkLabel",
        endRemarkLabelId = null,
        startDateErrorId = "startDateError",
        endDateErrorId = "endDateError",
        noDaysErrorId = "noDaysError",
        excludeLeaveId = null
    } = config;

    const form = document.getElementById(formId);
    if (!form) return;

    const leaveReason        = document.getElementById(leaveReasonId);
    const otherReasonField   = document.getElementById(otherReasonFieldId);
    const otherReasonInput   = document.getElementById(otherReasonInputId);
    const startDateInput     = document.getElementById(startDateId);
    const endDateInput       = document.getElementById(endDateId);
    const noDays             = document.getElementById(noDaysId);
    const remark             = document.getElementById(remarkId);
    const startRemark        = document.getElementById(startRemarkId);
    const endRemark          = document.getElementById(endRemarkId);
    const endRemarkField     = document.getElementById(endRemarkFieldId);

    // ── Flag ──────────────────────────────────────────────────────────────
    let hasDateClash = false;

    // ── Public Holidays ───────────────────────────────────────────────────
    let publicHolidays = [];
    fetch('/get_public_holidays')
        .then(r => r.json())
        .then(data => { publicHolidays = data; })
        .catch(err => console.error('Error fetching holidays:', err));

    function isPublicHoliday(dateStr) {
        return publicHolidays.find(h => h.date === dateStr);
    }

    // ── Min Date ──────────────────────────────────────────────────────────
    const today = new Date();
    const oneWeekAgo = new Date(today);
    oneWeekAgo.setDate(today.getDate() - 7);

    startDateInput.setAttribute("min", today);
    endDateInput.setAttribute("min", today);

    // ── Other Reason ──────────────────────────────────────────────────────
    leaveReason?.addEventListener("change", () => {
        const isOther = leaveReason.value === "Other";
        if (otherReasonField) otherReasonField.style.display = isOther ? "block" : "none";
        if (!isOther && otherReasonInput) otherReasonInput.value = "";
    });

    // ── Date Change Handlers ──────────────────────────────────────────────
    function handleDateChange(input) {
        const dateStr = input.value;
        const d = new Date(dateStr);
        const errorId = input.id === startDateId ? startDateErrorId : endDateErrorId;

        clearInvalid(input);
        hideErrorMessage(errorId);

        if (d < oneWeekAgo) {
            input.value = "";
            showInvalid(input);
            showErrorMessage(errorId, "Backdated leave cannot exceed 7 days.");
            return;
        }

        const holiday = isPublicHoliday(dateStr);
        if (holiday) {
            input.value = "";
            showInvalid(input);
            showErrorMessage(errorId, holiday.name);
            return;
        }

        if (!isWorkingDay(d)) {
            input.value = "";
            showInvalid(input);
            showErrorMessage(errorId, "Weekend (Saturday/Sunday)");
            return;
        }

        if (input === startDateInput) {
            endDateInput.value = "";
            endDateInput.setAttribute("min", startDateInput.value);
            hideErrorMessage(endDateErrorId);
            // Reset clash flag when start date changes
            hasDateClash = false;
        }

        updateRemarkFields();
    }

    startDateInput.addEventListener("change", () => handleDateChange(startDateInput));
    endDateInput.addEventListener("change",   () => handleDateChange(endDateInput));

    // ── Remark Visibility ─────────────────────────────────────────────────
    function updateRemarkFields() {
        if (!startDateInput.value || !endDateInput.value) return;

        if (startDateInput.value === endDateInput.value) {
            remark?.classList.remove("d-none");
            remark?.setAttribute("name", "remark");
            document.getElementById(remarkLabelId)?.classList.remove("d-none");

            startRemark?.classList.add("d-none");
            startRemark?.removeAttribute("name");
            document.getElementById(startRemarkLabelId)?.classList.add("d-none");

            endRemark?.classList.add("d-none");
            endRemark?.removeAttribute("name");
            if (endRemarkField) endRemarkField.classList.add("d-none");

            if (!remark.value) remark.value = "Whole Day";
        } else {
            remark?.classList.add("d-none");
            remark?.removeAttribute("name");
            document.getElementById(remarkLabelId)?.classList.add("d-none");

            startRemark?.classList.remove("d-none");
            startRemark?.setAttribute("name", "start_remark");
            document.getElementById(startRemarkLabelId)?.classList.remove("d-none");
            if (!startRemark.value) startRemark.value = "Whole Day";

            endRemark?.classList.remove("d-none");
            endRemark?.setAttribute("name", "end_remark");
            if (endRemarkField) endRemarkField.classList.remove("d-none");
            if (!endRemark.value) endRemark.value = "Whole Day";
        }

        calculateDays();
        checkDateClash();  // ← triggers after both dates are valid
    }

    [remark, startRemark, endRemark].forEach(el => el?.addEventListener("change", calculateDays));

    // ── Clash Check ───────────────────────────────────────────────────────
    async function checkDateClash() {
        const startDate = startDateInput.value;
        const endDate = endDateInput.value;
        if (!startDate || !endDate) return;

        const subordinateSelect = document.querySelector('[name="requester_emp_no"]');
        const employeeNo = subordinateSelect?.value || null;

        const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
        if (employeeNo) params.append('employee_no', employeeNo);

        const excludeLeaveId = config.excludeLeaveId || null;
        if (excludeLeaveId) params.append('exclude_leave_id', excludeLeaveId);

        try {
            const res  = await fetch(`/check_date?${params}`);
            const data = await res.json();

            if (data.clash) {
                hasDateClash = true;
                showInvalid(endDateInput);
                showErrorMessage(endDateErrorId, data.message);
                noDays.value = '';
            } else {
                hasDateClash = false;
                const endError = document.getElementById(endDateErrorId);
                if (endError?.innerHTML.includes('already exists')) {
                    hideErrorMessage(endDateErrorId);
                    clearInvalid(endDateInput);
                }
            }
        } catch (err) {
            console.error('Clash check failed:', err);
        }
    }

    // Submit Guard
    form.addEventListener('submit', function(e) {
        if (hasDateClash) {
            e.preventDefault();
            alert('You have a clashing leave request. Please select different dates.');
        }
    });

    // Calculate Days
    function calculateDays() {
        const s = new Date(startDateInput.value);
        const e = new Date(endDateInput.value);

        hideErrorMessage(noDaysErrorId);

        if (isNaN(s) || isNaN(e) || e < s) { noDays.value = ""; return; }

        const workingDays = calculateWorkingDays(s, e, publicHolidays);
        if (workingDays <= 0) {
            noDays.value = "";
            showInvalid(noDays);
            showErrorMessage(noDaysErrorId, "Selected date range contains no working days (excluding weekends and public holidays).");
            return;
        }

        clearInvalid(noDays);

        if (s.toDateString() === e.toDateString()) {
            noDays.value = remark.value.toLowerCase().includes("half day") ? "0.5" : "1";
            return;
        }

        const startVal = startRemark.value.toLowerCase().includes("half day") ? 0.5 : 1;
        const endVal   = endRemark.value.toLowerCase().includes("half day") ? 0.5 : 1;
        const middle   = Math.max(workingDays - 2, 0);
        noDays.value   = middle + startVal + endVal;
    }
}