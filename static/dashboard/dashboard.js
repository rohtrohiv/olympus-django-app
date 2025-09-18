function showSection(sectionId) {
    document.querySelectorAll('.dashboard-section').forEach(sec => {
        sec.classList.remove('active');
    });
    document.getElementById(sectionId).classList.add('active');
    document.querySelectorAll('nav a').forEach(link => {
        link.classList.remove('active');
        if (link.getAttribute('href').substring(1) === sectionId) {
            link.classList.add('active');
        }
    });
}
// Optionally, add more advanced interactivity here (charts, AJAX, etc.)
