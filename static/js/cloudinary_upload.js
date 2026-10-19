// ☁️ رفع الملفات مباشرة من المتصفح لـ Cloudinary (بدون المرور على سيرفر Render)
// الاستخدام: const url = await LabeebUpload.uploadFileDirect(file, 'اسم_المجلد');
(function () {
    const CLOUD_NAME = 'f4t8ayoq';
    const UPLOAD_PRESET = 'labeeb_files_unsigned';
    const MAX_MB = 10;

    async function compressImage(file, maxDim = 1600, quality = 0.82) {
        if (!/^image\/(jpeg|png)$/.test(file.type)) return file;
        try {
            const bitmap = await createImageBitmap(file);
            const scale = Math.min(1, maxDim / Math.max(bitmap.width, bitmap.height));
            const w = Math.round(bitmap.width * scale);
            const h = Math.round(bitmap.height * scale);
            const canvas = document.createElement('canvas');
            canvas.width = w; canvas.height = h;
            const c = canvas.getContext('2d');
            c.fillStyle = '#fff';
            c.fillRect(0, 0, w, h);
            c.drawImage(bitmap, 0, 0, w, h);
            if (bitmap.close) bitmap.close();
            const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', quality));
            if (!blob || blob.size >= file.size) return file;
            return new File([blob], file.name.replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' });
        } catch (e) {
            return file;
        }
    }

    async function uploadFileDirect(file, folder, retries = 2) {
        const en = document.documentElement.lang === 'en';
        const wait = (ms) => new Promise(r => setTimeout(r, ms));
        const netMsg = en
            ? 'Could not reach the upload service. Check your internet connection and try again.'
            : 'تعذر الاتصال بخدمة رفع الملفات. تأكد من اتصالك بالإنترنت وحاول مرة أخرى.';

        file = await compressImage(file);
        if (file.size > MAX_MB * 1024 * 1024) {
            throw new Error(en ? `The file is too large (max ${MAX_MB} MB).`
                               : `حجم الملف كبير جداً (الحد الأقصى ${MAX_MB} ميجابايت).`);
        }

        const endpoint = `https://api.cloudinary.com/v1_1/${CLOUD_NAME}/auto/upload`;

        for (let attempt = 0; attempt <= retries; attempt++) {
            let res;
            try {
                const fd = new FormData();
                fd.append('file', file);
                fd.append('upload_preset', UPLOAD_PRESET);
                if (folder) fd.append('folder', folder);
                res = await fetch(endpoint, { method: 'POST', body: fd });
            } catch (e) {
                if (attempt === retries) throw new Error(netMsg);
                await wait(2000 * (attempt + 1));
                continue;
            }

            let data = {};
            try { data = await res.json(); } catch (e) {}
            if (res.ok && data.secure_url) return data.secure_url;

            if ([502, 503, 504].includes(res.status) && attempt < retries) {
                await wait(2000 * (attempt + 1));
                continue;
            }

            const detail = (data.error && data.error.message) ? data.error.message : '';
            let base;
            if (/preset/i.test(detail)) {
                base = en ? 'Upload settings are incomplete (upload preset). Please contact support.'
                          : 'إعدادات الرفع غير مكتملة (Upload Preset)، تواصل مع الدعم الفني.';
            } else if (res.status === 413 || /size|large/i.test(detail)) {
                base = en ? 'The file is too large. Try a smaller file.'
                          : 'حجم الملف كبير جداً، جرّب ملفاً أصغر.';
            } else if (/format|type|not allowed|unsupported/i.test(detail)) {
                base = en ? 'This file type is not supported.'
                          : 'نوع الملف غير مدعوم.';
            } else {
                base = en ? `Upload failed (code ${res.status}).`
                          : `فشل رفع الملف (رمز الخطأ ${res.status}).`;
            }
            throw new Error(detail ? `${base} (${detail})` : base);
        }
    }

    window.LabeebUpload = { uploadFileDirect, compressImage };
})();