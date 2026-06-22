document.addEventListener("DOMContentLoaded", () => {
    initLeaveForm({
        formId: "leaveForm",
        leaveReasonId: null,
        otherReasonFieldId: null,
        otherReasonInputId: null,
        startDateId: "startDate",
        endDateId: "endDate",
        noDaysId: "noDays",
        remarkId: "remark",
        startRemarkId: "startRemark",
        endRemarkId: "endRemark",
        endRemarkFieldId: "endRemarkField"
    });
});