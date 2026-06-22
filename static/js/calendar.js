let currentDate = new Date();
let leaveData = [];

const monthNames = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'
];

const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function initializeCalendar() {
    loadLeaveData();
    renderCalendar();
}

function loadLeaveData() {
    fetch('/get_leaves')
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            leaveData = data;
            renderCalendar();
            renderListView();
        })
        .catch(error => {
            console.error('Error loading leaves:', error);
            leaveData = [];
            renderCalendar();
            renderListView();
        });
}

const MAX_VISIBLE = 3; // max indicators shown before "+N more"

function renderCalendar() {
    const grid = document.getElementById('calendarGrid');
    const monthYear = document.getElementById('monthYear');

    const year = currentDate.getFullYear();
    const month = currentDate.getMonth();

    monthYear.textContent = `${monthNames[month]} ${year}`;
    grid.innerHTML = '';

    dayNames.forEach(day => {
        const dayHeader = document.createElement('div');
        dayHeader.className = 'calendar-day-header';
        dayHeader.textContent = day;
        grid.appendChild(dayHeader);
    });

    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const today = new Date();

    for (let i = 0; i < firstDay; i++) {
        const emptyDay = document.createElement('div');
        emptyDay.className = 'calendar-day past';
        grid.appendChild(emptyDay);
    }

    for (let day = 1; day <= daysInMonth; day++) {
        const dayElement = document.createElement('div');
        dayElement.className = 'calendar-day';

        const currentDayDate = new Date(year, month, day);
        const isToday = currentDayDate.toDateString() === today.toDateString();
        const isPast = currentDayDate < today && !isToday;

        if (isToday) dayElement.classList.add('today');
        else if (isPast) dayElement.classList.add('past');

        const dayNumber = document.createElement('div');
        dayNumber.className = 'day-number';
        dayNumber.textContent = day;
        dayElement.appendChild(dayNumber);

        const dateString = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const dayLeaves = leaveData.filter(leave => leave.start === dateString);

        if (dayLeaves.length > 0) {
            const indicatorWrapper = document.createElement('div');
            indicatorWrapper.className = 'leave-indicators';

            const visible = dayLeaves.slice(0, MAX_VISIBLE);
            const overflow = dayLeaves.length - MAX_VISIBLE;

            visible.forEach(leave => {
                const indicator = document.createElement('div');
                indicator.className = `leave-indicator ${leave.className}`;

                const remark = (leave.remark || '').toLowerCase();
                let badgeHtml = '';
                if (remark.includes('1st half') || remark.includes('first half')) {
                    badgeHtml = '<span class="leave-half-badge">1st half</span>';
                } else if (remark.includes('2nd half') || remark.includes('second half')) {
                    badgeHtml = '<span class="leave-half-badge">2nd half</span>';
                }

                indicator.innerHTML = `${leave.title}${badgeHtml}`;
                indicatorWrapper.appendChild(indicator);
            });

            if (overflow > 0) {
                const more = document.createElement('div');
                more.className = 'leave-more';
                more.textContent = `+${overflow} more`;
                indicatorWrapper.appendChild(more);
            }

            dayElement.appendChild(indicatorWrapper);

            // Click to show popup with all entries
            dayElement.addEventListener('click', (e) => {
                showDayPopup(e, dateString, dayLeaves);
            });
        }

        grid.appendChild(dayElement);
    }
}

function showDayPopup(e, dateString, leaves) {
    closePopup();

    const popup = document.createElement('div');
    popup.className = 'day-popup active';
    popup.id = 'dayPopup';

    const dateLabel = new Date(dateString + 'T00:00:00').toLocaleDateString('en-US', {
        weekday: 'short', month: 'short', day: 'numeric'
    });

    popup.innerHTML = `
        <div class="day-popup-header">
            <span>${dateLabel}</span>
            <button class="day-popup-close" onclick="closePopup()">&#x2715;</button>
        </div>
        ${leaves.map(leave => {
            const remark = (leave.remark || '').toLowerCase();
            let halfLabel = '';
            if (remark.includes('1st half') || remark.includes('first half')) {
                halfLabel = '<span style="font-size:0.75rem; color:#3498db; margin-left:4px;">(1st half)</span>';
            } else if (remark.includes('2nd half') || remark.includes('second half')) {
                halfLabel = '<span style="font-size:0.75rem; color:#3498db; margin-left:4px;">(2nd half)</span>';
            }

            return `
                <div class="day-popup-item">
                    <div class="day-popup-dot ${leave.className}"></div>
                    <span>${leave.title}${halfLabel}</span>
                </div>
            `;
        }).join('')}
    `;

    document.body.appendChild(popup);

    // Position near click but keep within viewport
    const x = Math.min(e.clientX + 10, window.innerWidth - 320);
    const y = Math.min(e.clientY + 10, window.innerHeight - (leaves.length * 40 + 80));
    popup.style.left = `${x}px`;
    popup.style.top = `${y}px`;

    // Close when clicking outside
    setTimeout(() => {
        document.addEventListener('click', outsideClickClose);
    }, 0);
}

function outsideClickClose(e) {
    const popup = document.getElementById('dayPopup');
    if (popup && !popup.contains(e.target)) {
        closePopup();
    }
}

function closePopup() {
    const existing = document.getElementById('dayPopup');
    if (existing) existing.remove();
    document.removeEventListener('click', outsideClickClose);
}

function renderListView() {
    const listContainer = document.getElementById('leaveList');
    const loading = document.getElementById('listLoading');

    if (leaveData.length === 0) {
        loading.style.display = 'block';
        loading.innerHTML = '<i class="fas fa-info-circle"></i> No leaves or holidays found';
        listContainer.innerHTML = '';
        return;
    }

    loading.style.display = 'none';

    const groupedLeaves = {};

    leaveData.forEach(leave => {
        const leaveDate = new Date(leave.start + 'T00:00:00');
        const dateKey = leaveDate.toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'long',
            day: 'numeric',
            weekday: 'long'
        });

        if (!groupedLeaves[dateKey]) {
            groupedLeaves[dateKey] = [];
        }
        groupedLeaves[dateKey].push(leave);
    });

    const sortedDates = Object.keys(groupedLeaves).sort((a, b) => {
        return new Date(a) - new Date(b);
    });

    listContainer.innerHTML = sortedDates.map(date => {
        const leaves = groupedLeaves[date];
        return `
            <div class="list-item ${leaves[0].className || 'employee-leave'}">
                <div class="list-date">
                    <i class="fas fa-calendar-alt"></i>
                    ${date}
                </div>
                <div class="list-leaves">
                    ${leaves.map(leave => {
                        const tagClass = leave.className === 'public-holiday' 
                            ? 'holiday' 
                            : leave.className === 'recommended-leave'
                            ? 'recommended'
                            : 'employee';
                        return `<span class="list-leave-tag ${tagClass}">${leave.title}</span>`;
                    }).join('')}
                </div>
            </div>
        `;
    }).join('');
}

function previousMonth() {
    currentDate.setMonth(currentDate.getMonth() - 1);
    renderCalendar();
}

function nextMonth() {
    currentDate.setMonth(currentDate.getMonth() + 1);
    renderCalendar();
}

function goToToday() {
    currentDate = new Date();
    renderCalendar();
}

function switchView(view) {
    const calendarView = document.getElementById('calendarView');
    const listView = document.getElementById('listView');
    const viewBtns = document.querySelectorAll('.view-btn');
    
    viewBtns.forEach(btn => btn.classList.remove('active'));
    
    if (view === 'calendar') {
        calendarView.style.display = 'block';
        listView.classList.remove('active');
        viewBtns[0].classList.add('active');
    } else {
        calendarView.style.display = 'none';
        listView.classList.add('active');
        viewBtns[1].classList.add('active');
    }
}

// Initialize
document.addEventListener('DOMContentLoaded', initializeCalendar);