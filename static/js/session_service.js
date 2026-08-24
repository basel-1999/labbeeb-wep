// ==========================================
// 🔧 إعدادات Agora وخدمات الجلسة للمتصفح
// ==========================================
const SessionCheckService = {
    agoraEngine: null,
    isMuted: false,
    agoraAppId: '3a54c447b558405da3e87b1177ccc463',
    screenShareClient: null,
    localAudioTrack: null,

    // 🛠️ دالة جلب Token ديناميكي من سيرفر Node.js
    async fetchDynamicToken(channelName, uid) {
        try {
            const serverBaseUrl = 'https://agora-server-59qz.onrender.com';
            const response = await fetch(`${serverBaseUrl}/rtc-token?channelName=${channelName}&uid=${uid}`);
            if (response.ok) {
                const data = await response.json();
                return data.token || '';
            }
            console.error('Failed to get token from server:', response.status);
            return '';
        } catch (e) {
            console.error("Error fetching token from server:", e);
            return '';
        }
    },

    // 1. تهيئة محرك الاتصال الصوتي
    async initAudioEngine() {
        if (this.agoraEngine) return;
        try {
            // استخدام Agora Web SDK
            this.agoraEngine = AgoraRTC.createClient({ mode: "live", codec: "vp8" });
        } catch (e) {
            console.error("Agora initialization error:", e);
            this.agoraEngine = null;
        }
    },

    // 2. الانضمام إلى غرفة الصوت
    async joinAudioChannel(sessionId, token = null) {
        const user = firebase.auth().currentUser;
        // توليد UID رقمي بناءً على UID الخاص بـ Firebase
        const numericUid = user ? this.hashCode(user.uid) % 100000 : Math.floor(Date.now() % 100000);
        
        const activeToken = token || await this.fetchDynamicToken(sessionId, numericUid);

        if (!this.agoraEngine) {
            await this.initAudioEngine();
        }

        try {
            await this.agoraEngine.setClientRole("broadcaster");
            
            // اشتراك في الصوت
            this.agoraEngine.on("user-published", async (user, mediaType) => {
                if (mediaType === "audio") {
                    await this.agoraEngine.subscribe(user, mediaType);
                    const audioTrack = user.audioTrack;
                    audioTrack.play();
                }
            });

            await this.agoraEngine.join(activeToken, sessionId, null, numericUid);
            
            // نشر المايكروفون
            this.localAudioTrack = await AgoraRTC.createMicrophoneAudioTrack();
            await this.agoraEngine.publish([this.localAudioTrack]);
            
            this.isMuted = false;
        } catch (e) {
            console.error("Join Agora channel error:", e);
        }
    },

    // 2. أ - انضمام المدير للمراقبة بصمت تام
    async joinAudioChannelAsAdmin(sessionId, adminToken = null) {
        if (!this.agoraEngine) {
            await this.initAudioEngine();
        }
        
        const user = firebase.auth().currentUser;
        const adminNumericUid = user ? this.hashCode(user.uid) % 100000 : Math.floor(Date.now() % 100000);
        const activeAdminToken = adminToken || await this.fetchDynamicToken(sessionId, adminNumericUid);

        try {
            await this.agoraEngine.setClientRole("audience");
            
            this.agoraEngine.on("user-published", async (user, mediaType) => {
                if (mediaType === "audio" || mediaType === "video") {
                    await this.agoraEngine.subscribe(user, mediaType);
                    if (mediaType === "audio") user.audioTrack.play();
                }
            });

            await this.agoraEngine.join(activeAdminToken, sessionId, null, adminNumericUid);
            this.isMuted = true;
        } catch (e) {
            console.error("Join Agora channel as admin error:", e);
        }
    },

    // 3. كتم / تشغيل المايك
    async toggleMuteAudio() {
        if (!this.agoraEngine || !this.localAudioTrack) return false;
        this.isMuted = !this.isMuted;
        if (this.isMuted) {
            this.localAudioTrack.setMuted(true);
        } else {
            this.localAudioTrack.setMuted(false);
        }
        return this.isMuted;
    },

    // 3. ب - تفعيل أو إيقاف مشاركة الشاشة
    async toggleScreenShare(enable, sessionId = '', token = null) {
        try {
            if (enable) {
                const user = firebase.auth().currentUser;
                const numericUid = user ? this.hashCode(user.uid) % 100000 : Math.floor(Date.now() % 100000);
                // UID مختلف (+1) لكي لا يتعارض مع UID الصوت
                const screenShareUid = numericUid + 1;
                const activeToken = token || await this.fetchDynamicToken(sessionId, screenShareUid);

                this.screenShareClient = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
                await this.screenShareClient.join(activeToken, sessionId, null, screenShareUid);
                
                const screenTrack = await AgoraRTC.createScreenVideoTrack({
                    encoderConfig: "1080p_1",
                    optimizationMode: "detail"
                });
                await this.screenShareClient.publish(screenTrack);
                return true;
            } else {
                if (this.screenShareClient) {
                    await this.screenShareClient.leave();
                    this.screenShareClient = null;
                }
                return false;
            }
        } catch (e) {
            console.error("Web Screen Share Error:", e);
            return !enable;
        }
    },

    // 4. مغادرة قناة الصوت
    async leaveAndReleaseAudio() {
        if (this.agoraEngine) {
            try {
                await this.agoraEngine.leave();
            } catch (e) {
                console.error("Release Agora error:", e);
            } finally {
                this.agoraEngine = null;
                this.isMuted = false;
            }
        }
        if (this.screenShareClient) {
            try { await this.screenShareClient.leave(); } catch (e) {}
            this.screenShareClient = null;
        }
    },

    // ==========================================
    // 🎤 فحوصات الشبكة والمايك
    // ==========================================
    async checkMicrophone() {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            stream.getTracks().forEach(track => track.stop());
            return true;
        } catch (e) {
            console.error("Microphone access error:", e);
            return false;
        }
    },

    async checkNetworkLatency() {
        if (!navigator.onLine) return -1;

        return new Promise((resolve) => {
            const img = new Image();
            const startTime = Date.now();
            const cacheBuster = Date.now();
            img.src = `https://1.1.1.1/favicon.ico?v=${cacheBuster}`;

            img.onload = () => resolve(Date.now() - startTime);
            img.onerror = () => {
                const latency = Date.now() - startTime;
                resolve(latency > 0 ? latency : 120);
            };

            // Timeout بعد 3 ثوانٍ
            setTimeout(() => resolve(-1), 3000);
        });
    },

    // ==========================================
    // 🔗 دوال مساعدة (String to HashCode for UID)
    // ==========================================
    hashCode(str) {
        let hash = 0;
        if (str.length == 0) return hash;
        for (let i = 0; i < str.length; i++) {
            const char = str.charCodeAt(i);
            hash = ((hash << 5) - hash) + char;
            hash = hash & hash; // Convert to 32bit integer
        }
        return Math.abs(hash);
    }
};