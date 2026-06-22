function showInvalid(input) {
    input.classList.add("is-invalid");
}

function clearInvalid(input) {
    input.classList.remove("is-invalid");
}

document.addEventListener("DOMContentLoaded", function () {
    const leaveReason = document.getElementById("leaveReason");
    const otherReasonField = document.getElementById("otherReasonField");
    const otherReasonInput = document.getElementById("leaveReasonOther");

    const startDateInput = document.getElementById("startDate");
    const endDateInput = document.getElementById("endDate");
    const noDays = document.getElementById("noDays");

    const remark = document.getElementById("remark");
    const startRemark = document.getElementById("startRemark");
    const endRemark = document.getElementById("endRemark");
    const endRemarkField = document.getElementById("endRemarkField");

    // Set today's date as the minimum allowed date
    const today = new Date().toISOString().split("T")[0];
    startDateInput.setAttribute("min", today);
    endDateInput.setAttribute("min", today);

    // Handle "Other" leave reason visibility
    leaveReason.addEventListener("change", function () {
        otherReasonField.style.display = this.value === "Other" ? "block" : "none";
        if (this.value !== "Other") {
            otherReasonInput.value = ""; // Clear field if not selected
        }
    });

    // Prevent form submission if "Other" is selected without input
    document.getElementById("leaveForm").addEventListener("submit", function (event) {
        if (leaveReason.value === "Other") {
            if (!otherReasonInput.value.trim()) {
                alert("Please specify the reason for 'Other' leave type.");
                event.preventDefault();
                return false;
            }
        
            const tempInput = document.createElement("input");
            tempInput.type = "hidden";
            tempInput.name = "reason";
            tempInput.value = otherReasonInput.value.trim();
            this.appendChild(tempInput);
            leaveReason.disabled = true;
        }
    });

    // Date input change event
    startDateInput.addEventListener("change", function () {
        const selectedDate = new Date(this.value);

        if (!isWorkingDay(selectedDate)) {
            this.value = "";
            showInvalid(this);
            return;
        }

        endDateInput.value = "";
        endDateInput.removeAttribute("disabled");
        endDateInput.setAttribute("min", this.value);
        updateRemarkFields();
    });

    endDateInput.addEventListener("change", function () {
        const selectedDate = new Date(this.value);

        clearInvalid(this);

        if (!isWorkingDay(selectedDate)) {
            this.value = "";
            showInvalid(this);
            return;
        }

        updateRemarkFields();
    });

    startRemark.addEventListener("change", calculateDays);
    endRemark.addEventListener("change", calculateDays);
    remark.addEventListener("change", calculateDays);

    function updateRemarkFields() {
        let startDate = startDateInput.value;
        let endDate = endDateInput.value;

        if (startDate && endDate) {
            if (startDate === endDate) {
                // Show "Remark" and hide Start/End Remarks
                remark.classList.remove("d-none");
                remark.setAttribute("name", "remark");
                document.getElementById("remarkLabel").classList.remove("d-none");

                startRemark.classList.add("d-none");
                startRemark.removeAttribute("name");
                document.getElementById("startRemarkLabel").classList.add("d-none");

                endRemark.classList.add("d-none");
                endRemark.removeAttribute("name");
                endRemarkField.classList.add("d-none");
            } else {
                // Hide "Remark" and show Start/End Remarks
                remark.classList.add("d-none");
                remark.removeAttribute("name");
                document.getElementById("remarkLabel").classList.add("d-none");

                startRemark.classList.remove("d-none");
                startRemark.setAttribute("name", "start_remark");
                document.getElementById("startRemarkLabel").classList.remove("d-none");

                endRemark.classList.remove("d-none");
                endRemark.setAttribute("name", "end_remark");
                endRemarkField.classList.remove("d-none");
            }
        }
        calculateDays();
    }

    function calculateWorkingDays(startDate, endDate) {
        let count = 0;
        let current = new Date(startDate);

        while (current <= endDate) {
            const day = current.getDay();
            if (day !== 0 && day !== 6) {
                count++;
            }
            current.setDate(current.getDate() + 1);
        }

        return count;
    }

    function isWorkingDay(date) {
        const day = date.getDay();
        return day !== 0 && day !== 6;
    }

    function calculateDays() {
        let startDate = new Date(startDateInput.value);
        let endDate = new Date(endDateInput.value);

        if (isNaN(startDate) || isNaN(endDate)) {
            noDays.value = "";
            return;
        }
    
        if (endDate < startDate) {
            alert("End date must be on or after Start date.");
            endDateInput.value = "";
            noDays.value = "";
            return;
        }

        // Working days only
        let workingDays = calculateWorkingDays(startDate, endDate);

        if (workingDays <= 0) {
            alert("Selected date range contains no working days.");
            noDays.value = "";
            return;
        }

        // Same-day leave
        if (startDate.toDateString() === endDate.toDateString()) {
            if (!isWorkingDay(startDate)) {
                alert("Selected date is not a working day.");
                noDays.value = "";
                return;
            }
            remark.classList.remove("d-none");
            noDays.value = remark.value.includes("0.5") ? "0.5" : "1";
            return;
        }

        // Multi-day leave
        let startValue = (isWorkingDay(startDate) && startRemark.value.includes("0.5")) ? 0.5 : 1;
        let endValue = (isWorkingDay(endDate) && endRemark.value.includes("0.5")) ? 0.5 : 1;

        // Middle working days (excluding first & last)
        let middleDays = Math.max(workingDays - 2, 0);

        noDays.value = middleDays + startValue + endValue;
    }

    // Form validation
    document.getElementById("leaveForm").addEventListener("submit", function (event) {
        const requiredFields = document.querySelectorAll('[required]');
        let isValid = true;

        for (let field of requiredFields) {
            if (field.value.trim() === "") {
                isValid = false;
                break;
            }
        }

        if (!isValid) {
            event.preventDefault();
            alert("Please fill in all required fields.");
        } else {
            alert ("Leave application submitted successfully!");
        }
    });
});