// 👁️ بيضيف أيقونة عين (إظهار/إخفاء) على كل خانة كلمة مرور بالصفحة تلقائياً
(function () {
    const EYE = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>';
    const EYE_OFF = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21"/></svg>';

    function enhance(input) {
        if (input.dataset.pwToggle === '1') return;
        input.dataset.pwToggle = '1';

        const wrap = document.createElement('div');
        wrap.style.position = 'relative';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        input.style.paddingInlineEnd = '2.75rem'; // عشان النص ما يدخل تحت الأيقونة

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.title = 'إظهار / إخفاء كلمة المرور';
        btn.setAttribute('aria-label', 'إظهار أو إخفاء كلمة المرور');
        btn.style.cssText = 'position:absolute;top:50%;inset-inline-end:12px;transform:translateY(-50%);background:none;border:0;padding:4px;cursor:pointer;color:#6B7280;display:flex;align-items:center;';
        btn.innerHTML = EYE;
        btn.addEventListener('click', function () {
            const show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            btn.innerHTML = show ? EYE_OFF : EYE;
            btn.style.color = show ? '#4E6B45' : '#6B7280';
            input.focus();
        });
        wrap.appendChild(btn);
    }

    function init() {
        document.querySelectorAll('input[type="password"]').forEach(enhance);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();