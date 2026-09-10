let isSessionExpired = false;

async function fetchWithAuth(url, options = {}) {
    // ใช้ sessionStorage ป้องกันปัญหาสลับแท็บแล้วหลุด
    const token = sessionStorage.getItem("access_token");
    const headers = { ...options.headers };
    
    if (token && token !== "undefined" && token !== "null") {
        headers['Authorization'] = `Bearer ${token}`;
    } else {
        console.warn("ไม่พบ Token สำหรับยิง API ไปที่:", url);
    }

    const response = await fetch(url, { ...options, headers });

    if (response.status === 401) {
        if (!isSessionExpired) {
            isSessionExpired = true; 
            handleLogout();
            showCustomAlert('warning', 'เซสชันหมดอายุ', 'กรุณาเข้าสู่ระบบใหม่อีกครั้ง');
        }
        throw new Error("Unauthorized");
    } 
    else if (response.status === 403) {
        const resData = await response.json().catch(() => ({}));
        if (resData.detail === "บัญชีของคุณกำลังรอ Admin อนุมัติการเข้าใช้งาน") {
            handleLogout();
            showCustomAlert('warning', 'รอการอนุมัติ', resData.detail);
        } else {
            showCustomAlert('warning', 'ไม่มีสิทธิ์เข้าถึง', 'คุณไม่มีสิทธิ์ใช้งานในส่วนนี้ (อาจเกิดจากสลับ User)');
        }
        throw new Error("Forbidden");
    }
    return response;
}

let palletCount = 0, ocrExpectedQty = 0, totalActualPiecesGlobal = 0;
let isOcrScanned = false, currentUser = "";
let currentDocNumber = "", currentImageBase64 = "", currentCropImageBase64 = "";
let currentPlantTicketcode = ""; 

let audioCtx;
let editingRecordId = null; 
let currentOcrFullData = {}; 

let globalAdminData = [];
let isDailyDiscrepancyFilterOn = false;

let allPallets = [];
let selectedPalletValue = ""; 

let currentScaleImageBase64 = "";
let adminWs = null;

document.getElementById('userId').addEventListener('keypress', function(event) {
    if (event.key === 'Enter') { event.preventDefault(); handleLogin(); }
});
document.getElementById('password').addEventListener('keypress', function(event) {
    if (event.key === 'Enter') { event.preventDefault(); handleLogin(); }
});

document.addEventListener('click', function(event) {
    const trigger = document.getElementById('customSelectTrigger');
    const menu = document.getElementById('customSelectMenu');
    if (trigger && menu && !trigger.contains(event.target) && !menu.contains(event.target)) {
        menu.classList.add('hidden');
        if(document.getElementById('customSelectIcon')) {
            document.getElementById('customSelectIcon').classList.remove('rotate-180');
        }
    }
});

function togglePassword(inputId, iconId) {
    const pwd = document.getElementById(inputId);
    const icon = document.getElementById(iconId);
    if (pwd.type === 'password') {
        pwd.type = 'text'; icon.classList.replace('fa-eye', 'fa-eye-slash');
    } else {
        pwd.type = 'password'; icon.classList.replace('fa-eye-slash', 'fa-eye');
    }
}

function toggleAuthMode() {
    const loginSec = document.getElementById('loginSection');
    const regSec = document.getElementById('registerSection');
    const mainTitle = document.getElementById('authMainTitle');
    const subTitle = document.getElementById('authSubTitle');

    if (loginSec.classList.contains('hidden')) {
        loginSec.classList.remove('hidden');
        regSec.classList.add('hidden');
        mainTitle.innerText = "ระบบตรวจสอบพาเลทและห้องชั่ง";
        subTitle.innerText = "กรุณาเข้าสู่ระบบเพื่อยืนยันตัวตน";
    } else {
        loginSec.classList.add('hidden');
        regSec.classList.remove('hidden');
        mainTitle.innerText = "ลงทะเบียนพนักงานใหม่";
        subTitle.innerText = "สมัครใช้งานระบบ (ต้องรอ Admin อนุมัติ)";
    }
}

let currentUserView = 'pending'; // เก็บสถานะว่ากำลังดูแท็บไหนอยู่

function switchUserTab(tab) {
    currentUserView = tab;
    const btnPending = document.getElementById('subTabPending');
    const btnApproved = document.getElementById('subTabApproved');
    const title = document.getElementById('userTableTitle');

    if (tab === 'pending') {
        btnPending.className = "bg-white text-gray-800 shadow-sm px-5 py-2 rounded-md font-bold text-sm transition-all flex items-center";
        btnApproved.className = "text-gray-500 hover:text-gray-700 px-5 py-2 rounded-md font-bold text-sm transition-all flex items-center";
        title.innerHTML = '<i class="fa-solid fa-user-clock mr-2"></i> รายชื่อพนักงานรออนุมัติ';
    } else {
        btnApproved.className = "bg-white text-gray-800 shadow-sm px-5 py-2 rounded-md font-bold text-sm transition-all flex items-center";
        btnPending.className = "text-gray-500 hover:text-gray-700 px-5 py-2 rounded-md font-bold text-sm transition-all flex items-center";
        title.innerHTML = '<i class="fa-solid fa-users mr-2"></i> รายชื่อพนักงานที่ใช้งานอยู่';
    }
    refreshUserTable();
}

function refreshUserTable() {
    if (currentUserView === 'pending') {
        loadUsersData('/auth/users/pending', 'pending');
    } else {
        // คุณต้องสร้าง API ยิงไปที่ /auth/users/approved (หรือ /auth/users แล้วกรอง) ในฝั่ง FastAPI เพิ่มเติมนะครับ
        loadUsersData('/auth/users/approved', 'approved'); 
    }
}

// ฟังก์ชันโหลดข้อมูลอเนกประสงค์ (ใช้ร่วมกันทั้ง 2 แท็บ)
async function loadUsersData(apiUrl, viewType) {
    const tbody = document.getElementById('usersTableBody');
    tbody.innerHTML = Array(4).fill().map(() => `
        <tr class="animate-pulse bg-white border-b border-gray-100">
            <td class="px-4 py-4"><div class="h-4 bg-gray-200 rounded w-8"></div></td>
            <td class="px-4 py-4"><div class="h-4 bg-gray-200 rounded w-24"></div></td>
            <td class="px-4 py-4"><div class="h-4 bg-gray-200 rounded w-32"></div></td>
            <td class="px-4 py-4 flex justify-center"><div class="h-6 bg-gray-200 rounded w-16"></div></td>
            <td class="px-4 py-4"><div class="h-8 bg-gray-200 rounded w-full max-w-[120px] mx-auto"></div></td>
        </tr>
    `).join('');

    try {
        const response = await fetchWithAuth(apiUrl);
        const result = await response.json();
        
        if(!result.success || result.data.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="text-center text-gray-500 py-10">${viewType === 'pending' ? 'ไม่มีพนักงานรอการอนุมัติ' : 'ไม่พบข้อมูลผู้ใช้งาน'}</td></tr>`;
            return;
        }

        let html = '';
        result.data.forEach(u => {
            const roleBadge = u.role === 'SCALE' ? 'bg-teal-100 text-teal-800' : 'bg-blue-100 text-blue-800';
            
            // UI ปุ่มจัดการจะต่างกันตาม View
            let actionButtons = '';
            if (viewType === 'pending') {
                actionButtons = `
                    <div class="flex items-center justify-center gap-2" id="action-container-${u.id}">
                        <button onclick="approveUser(${u.id})" class="bg-green-600 hover:bg-green-700 text-white px-3 py-1.5 rounded-lg font-bold text-sm shadow-sm transition-all active:scale-95">
                            <i class="fa-solid fa-check mr-1"></i> อนุมัติ
                        </button>
                        <button onclick="rejectUser(${u.id})" class="bg-white border border-red-300 text-red-500 hover:bg-red-50 px-3 py-1.5 rounded-lg font-bold text-sm shadow-sm transition-all active:scale-95">
                            <i class="fa-solid fa-xmark mr-1"></i> ปฏิเสธ
                        </button>
                    </div>`;
            } else {
                // UI สำหรับคนที่อนุมัติแล้ว (เช่น ระงับสิทธิ์ชั่วคราว หรือรีเซ็ตรหัสผ่าน)
                actionButtons = `
                    <div class="flex items-center justify-center gap-2">
                        <button onclick="suspendUser(${u.id})" class="bg-white border border-gray-300 text-gray-600 hover:bg-gray-100 px-3 py-1.5 rounded-lg font-bold text-sm shadow-sm transition-all active:scale-95" title="ระงับสิทธิ์การใช้งาน">
                            <i class="fa-solid fa-ban mr-1"></i> ระงับสิทธิ์
                        </button>
                    </div>`;
            }

            html += `
                <tr class="hover:bg-gray-50 transition-colors border-b border-gray-100">
                    <td class="px-4 py-3 font-medium text-gray-500">${u.id}</td>
                    <td class="px-4 py-3 font-bold text-gray-800">${u.username}</td>
                    <td class="px-4 py-3 text-gray-700 font-medium">${u.first_name || '-'} ${u.last_name || '-'}</td>
                    <td class="px-4 py-3 text-center"><span class="${roleBadge} px-2 py-1 rounded font-bold text-xs">${u.role}</span></td>
                    <td class="px-4 py-3 text-center">${actionButtons}</td>
                </tr>
            `;
        });
        tbody.innerHTML = html;
    } catch(e) {
        if(e.message !== "Unauthorized") {
            console.error("โหลดรายการพนักงานไม่สำเร็จ", e);
            tbody.innerHTML = `<tr><td colspan="5" class="text-center text-red-500 py-10">เกิดข้อผิดพลาดในการโหลดข้อมูล</td></tr>`;
        }
    }
}

// อัปเดตตอนกด Tab จัดการผู้ใช้งานใน Navigation หลัก ให้แสดงสถานะเดิมหรือสถานะ Pending
// ไปแก้ไขในฟังก์ชัน switchAdminTab(tabName) ตรงบรรทัดที่เกี่ยวกับ 'users' ให้เรียก refreshUserTable()

async function suspendUser(userId) {
    if(!confirm("ยืนยันการระงับสิทธิ์ผู้ใช้งานนี้ใช่หรือไม่?")) return;
    
    try {
        const response = await fetchWithAuth(`/auth/users/suspend/${userId}`, { method: 'PUT' });
        const result = await response.json();
        if(result.success) {
            showCustomAlert('success', 'สำเร็จ', result.message);
            refreshUserTable(); // โหลดตารางใหม่
        } else {
            showCustomAlert('warning', 'ผิดพลาด', result.detail);
        }
    } catch(e) {
        if(e.message !== "Unauthorized") console.error(e);
    }
}

async function rejectUser(userId) {
    if(!confirm("คุณต้องการปฏิเสธคำขอและลบผู้ใช้นี้ออกจากระบบใช่หรือไม่?")) return;
    
    try {
        const response = await fetchWithAuth(`/auth/users/${userId}`, { method: 'DELETE' });
        const result = await response.json();
        if(result.success) {
            showCustomAlert('success', 'ลบผู้ใช้สำเร็จ', result.message);
            refreshUserTable(); // โหลดตารางใหม่
        } else {
            showCustomAlert('warning', 'ผิดพลาด', result.detail);
        }
    } catch(e) {
        if(e.message !== "Unauthorized") console.error(e);
    }
}

async function handleRegister() {
    const userId = document.getElementById('regUserId').value.trim();
    const password = document.getElementById('regPassword').value;
    const confirmPassword = document.getElementById('regConfirmPassword').value;
    const role = document.getElementById('regRole').value;
    const firstName = document.getElementById('regFirstName').value.trim();
    const lastName = document.getElementById('regLastName').value.trim();

    if(!userId || !password || !confirmPassword || !firstName || !lastName)  { 
        showCustomAlert('warning', 'ข้อมูลไม่ครบ', 'กรุณากรอกรหัสพนักงานและรหัสผ่าน'); 
        return; 
    }

    if(password !== confirmPassword){
        showCustomAlert('warning', 'รหัสผ่านไม่ตรงกัน', 'กรุณายืนยันรหัสผ่านให้ตรงกับรหัสผ่านที่ตั้งไว้');
        return;
    }

    const passwordRegex = /^(?=.*[a-zA-Z])(?=.*\d)(?=.*[^a-zA-Z0-9]).{8,}$/;
    if (!passwordRegex.test(password)) {
        showCustomAlert('warning', 'รหัสผ่านไม่ปลอดภัย', 'รหัสผ่านต้องยาว 8 ตัวอักษรขึ้นไป และต้องประกอบด้วยภาษาอังกฤษ ตัวเลข และอักขระพิเศษรวมอยู่ด้วยครับ');
        return;
    }

    const btn = document.getElementById('btnRegister');
    const originalContent = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add('opacity-75', 'cursor-not-allowed');
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i> กำลังลงทะเบียน...';

    try {
        const response = await fetch('/auth/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                username: userId,
                password: password,
                role: role,
                first_name: firstName,
                last_name: lastName 
            })
        });
        
        const data = await response.json();

        if (response.ok) {
            showCustomAlert('success', 'สมัครสำเร็จ!', 'ระบบได้รับข้อมูลของคุณแล้ว กรุณารอ Admin อนุมัติก่อนจึงจะเข้าสู่ระบบได้');
            document.getElementById('regUserId').value = '';
            document.getElementById('regPassword').value = '';
            document.getElementById('regConfirmPassword').value = '';
            document.getElementById('regFirstName').value = '';
            document.getElementById('regLastName').value = '';
            toggleAuthMode();
        } else {
            showCustomAlert('warning', 'ไม่สามารถสมัครได้', data.detail || 'เกิดข้อผิดพลาดบางอย่าง');
        }
    } catch (error) {
        console.error("Register Error:", error);
        showCustomAlert('warning', 'ข้อผิดพลาด', 'ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์ได้');
    } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-75', 'cursor-not-allowed');
        btn.innerHTML = originalContent;
    }
}

async function handleLogin() {
    const userId = document.getElementById('userId').value.trim();
    const password = document.getElementById('password').value;

    if(!userId || !password)  { 
        showCustomAlert('warning', 'ข้อมูลไม่ครบ', 'กรุณากรอกรหัสพนักงานและรหัสผ่านครับ'); 
        return; 
    }

    const btn = document.getElementById('btnLogin');
    const originalContent = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add('opacity-75', 'cursor-not-allowed');
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i> กำลังเข้าสู่ระบบ...';

    const formData = new FormData();
    formData.append("username", userId);
    formData.append("password", password);

    try {
        const response = await fetch('/auth/login', {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();

        if (response.ok) {
            sessionStorage.setItem("access_token", data.access_token);
            isSessionExpired = false;
            
            if(!audioCtx) {
                const AudioContext = window.AudioContext || window.webkitAudioContext;
                audioCtx = new AudioContext();
            }
            
            currentUser = userId;
            document.getElementById('loginPage').classList.add('hidden');

            const userRole = data.role.toUpperCase();

            if (userRole === 'ADMIN') {
                document.getElementById('adminPage').classList.remove('hidden');
                const now = new Date();
                const currentMonthValue = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
                document.getElementById('reportMonthPicker').value = currentMonthValue;
                
                const todayISO = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
                if(document.getElementById('adminDatePicker')) {
                    document.getElementById('adminDatePicker').value = todayISO;
                }
                switchAdminTab('daily');
                initAdminDashboard();
            } else if (userRole === 'SCALE') {
                document.getElementById('displayScaleUser').innerHTML = `<i class="fa-solid fa-user mr-1"></i> ${currentUser}`;
                document.getElementById('scalePage').classList.remove('hidden');
                
                const now = new Date();
                const currentMonthValue = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
                if(document.getElementById('scaleReportMonthPicker')) document.getElementById('scaleReportMonthPicker').value = currentMonthValue;
                
                const todayISO = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
                if(document.getElementById('scaleDatePicker')) {
                    document.getElementById('scaleDatePicker').value = todayISO;
                }
                switchScaleTab('scale');
                initAdminDashboard();
            } else { 
                document.getElementById('displayUser').innerHTML = `<i class="fa-solid fa-user mr-1"></i> ${currentUser}`;
                document.getElementById('checkerPage').classList.remove('hidden');
            }
        } else {
            showCustomAlert('warning', 'เข้าสู่ระบบล้มเหลว', data.detail || 'รหัสพนักงานหรือรหัสผ่านไม่ถูกต้อง');
        }
    } catch (error) {
        console.error("Login Error:", error);
        showCustomAlert('warning', 'ข้อผิดพลาด', 'ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์ได้');
    } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-75', 'cursor-not-allowed');
        btn.innerHTML = originalContent;
    }
}

function handleLogout() {
    if (adminWs){
        adminWs.close();
        adminWs = null;
    }
    sessionStorage.removeItem("access_token");
    currentUser = "";

    executeRetakeImage(); 
    resetScaleImage(); 
    document.getElementById('userId').value = '';
    document.getElementById('password').value = ''; 
    document.getElementById('checkerPage').classList.add('hidden');
    document.getElementById('checkerHistoryPage').classList.add('hidden');
    document.getElementById('adminPage').classList.add('hidden');
    document.getElementById('scalePage').classList.add('hidden');
    document.getElementById('loginPage').classList.remove('hidden');
    
    const loginSec = document.getElementById('loginSection');
    if (loginSec.classList.contains('hidden')) {
        toggleAuthMode();
    }
}

function previewScaleImage(event) {
    const file = event.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = function(e) {
        currentScaleImageBase64 = e.target.result;
        document.getElementById('scalePreviewImage').src = currentScaleImageBase64;
        document.getElementById('scaleBeforeScan').classList.add('hidden');
        document.getElementById('scaleAfterScan').classList.remove('hidden');
    };
    reader.readAsDataURL(file);
}

function resetScaleImage() {
    const scaleCameraInput = document.getElementById('scaleCameraInput');
    if (scaleCameraInput) scaleCameraInput.value = "";
    
    const scalePreviewImage = document.getElementById('scalePreviewImage');
    if (scalePreviewImage) scalePreviewImage.src = "";
    
    currentScaleImageBase64 = "";

    const beforeScan = document.getElementById('scaleBeforeScan');
    const afterScan = document.getElementById('scaleAfterScan');
    
    if (beforeScan && afterScan) {
        beforeScan.classList.remove('hidden');
        afterScan.classList.add('hidden');
    }
}

async function saveScaleTruckImage() {
    const licensePlate = document.getElementById('scaleLicensePlateInput').value.trim();
    if (!licensePlate) {
        showCustomAlert('warning', 'ข้อมูลไม่ครบ', 'กรุณากรอกทะเบียนรถก่อนบันทึกครับ');
        return;
    }
    if (!currentScaleImageBase64) {
        showCustomAlert('warning', 'ไม่มีรูปภาพ', 'กรุณาถ่ายรูปภาพก่อนบันทึกครับ');
        return;
    }

    const btnSave = document.getElementById('btnSaveScaleImage');
    const originalContent = btnSave.innerHTML;
    btnSave.disabled = true;
    btnSave.classList.add('opacity-75', 'cursor-not-allowed');
    btnSave.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i> กำลังบันทึก...';

    try {
        const response = await fetchWithAuth('/api/save-truck-image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                license_plate: licensePlate,
                image_base64: currentScaleImageBase64,
                operator_name: currentUser
            })
        });
        const res = await response.json();

        if (res.success) {
            showCustomAlert('success', 'สำเร็จ!', res.message);
            document.getElementById('scaleLicensePlateInput').value = "";
            resetScaleImage();
        } else {
            showCustomAlert('warning', 'บันทึกไม่สำเร็จ', res.message);
        }
    } catch (err) {
        if (err.message === "Unauthorized") return;
        console.error(err);
        showCustomAlert('warning', 'เกิดข้อผิดพลาด', 'ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์ได้');
    } finally {
        btnSave.disabled = false;
        btnSave.classList.remove('opacity-75', 'cursor-not-allowed');
        btnSave.innerHTML = originalContent;
    }
}

let selectedFileForCrop = null;
let cropImgElement = new Image();
let canvasPts = [];
let dragPtIndex = -1;
let scaleRatio = 1;

async function processImage(event) {
    const file = event.target.files[0];
    if (!file) return;

    selectedFileForCrop = file;
    cropImgElement.onload = function() { openCropModal(); };
    cropImgElement.src = URL.createObjectURL(file);
}

function manipulateImage(action) {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    
    if (action === 'rotateL' || action === 'rotateR') {
        canvas.width = cropImgElement.height;
        canvas.height = cropImgElement.width;
        ctx.translate(canvas.width / 2, canvas.height / 2);
        ctx.rotate((action === 'rotateR' ? 90 : -90) * Math.PI / 180);
    } else if (action === 'flipH') {
        canvas.width = cropImgElement.width;
        canvas.height = cropImgElement.height;
        ctx.translate(canvas.width / 2, canvas.height / 2);
        ctx.scale(-1, 1);
    }

    ctx.drawImage(cropImgElement, -cropImgElement.width / 2, -cropImgElement.height / 2);

    cropImgElement.onload = function() {
        openCropModal();
    };
    
    const dataUrl = canvas.toDataURL("image/jpeg", 0.9);
    cropImgElement.src = dataUrl;
    
    canvas.toBlob((blob) => {
        selectedFileForCrop = new File([blob], "manipulated_image.jpg", { type: "image/jpeg" });
    }, "image/jpeg", 0.9);
}

function detectDocumentCorners(imageElement, canvasWidth, canvasHeight) {
    let src = cv.imread(imageElement);
    
    let maxDim = Math.max(src.cols, src.rows);
    let processScale = maxDim > 800 ? 800 / maxDim : 1.0;
    let processSize = new cv.Size(src.cols * processScale, src.rows * processScale);
    
    let resized = new cv.Mat();
    cv.resize(src, resized, processSize, 0, 0, cv.INTER_AREA);

    let gray = new cv.Mat();
    let blurred = new cv.Mat();
    let edges = new cv.Mat();
    let dilated = new cv.Mat();

    cv.cvtColor(resized, gray, cv.COLOR_RGBA2GRAY, 0);
    cv.GaussianBlur(gray, blurred, new cv.Size(5, 5), 0, 0, cv.BORDER_DEFAULT);
    cv.Canny(blurred, edges, 75, 200, 3, false);

    let M = cv.Mat.ones(3, 3, cv.CV_8U);
    cv.dilate(edges, dilated, M, new cv.Point(-1, -1), 1, cv.BORDER_CONSTANT, cv.morphologyDefaultBorderValue());

    let contours = new cv.MatVector();
    let hierarchy = new cv.Mat();
    cv.findContours(dilated, contours, hierarchy, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE);

    let maxArea = 0;
    let bestApprox = new cv.Mat();
    let found = false;

    for (let i = 0; i < contours.size(); ++i) {
        let cnt = contours.get(i);
        let area = cv.contourArea(cnt);

        if (area > 3000) { 
            let approx = new cv.Mat();
            let epsilon = 0.02 * cv.arcLength(cnt, true);
            cv.approxPolyDP(cnt, approx, epsilon, true);

            if (approx.rows === 4 && area > maxArea) {
                maxArea = area;
                approx.copyTo(bestApprox);
                found = true;
            }
            approx.delete();
        }
    }

    let detectedPoints = null;

    if (found) {
        detectedPoints = [];
        let scaleX = canvasWidth / (src.cols * processScale);
        let scaleY = canvasHeight / (src.rows * processScale);

        for (let i = 0; i < 4; i++) {
            detectedPoints.push({
                x: bestApprox.data32S[i * 2] * scaleX,
                y: bestApprox.data32S[i * 2 + 1] * scaleY
            });
        }
        detectedPoints = sortCorners(detectedPoints);
    }

    src.delete(); resized.delete(); gray.delete(); blurred.delete(); 
    edges.delete(); dilated.delete(); M.delete(); contours.delete(); 
    hierarchy.delete(); bestApprox.delete();

    return detectedPoints;
}

function sortCorners(pts) {
    pts.sort((a, b) => a.y - b.y);
    let top = pts.slice(0, 2);
    let bottom = pts.slice(2, 4);
    top.sort((a, b) => a.x - b.x);
    bottom.sort((a, b) => b.x - a.x);
    return [top[0], top[1], bottom[0], bottom[1]];
}

function openCropModal() {
    document.getElementById('cropInteractiveModal').classList.remove('hidden');
    
    setTimeout(() => {
        const canvas = document.getElementById('docCanvas');
        const ctx = canvas.getContext('2d');
        const container = document.getElementById('canvasContainer');

        let maxWidth = container.clientWidth || window.innerWidth * 0.9;
        let maxHeight = container.clientHeight || window.innerHeight * 0.7;

        scaleRatio = Math.min(maxWidth / cropImgElement.width, maxHeight / cropImgElement.height);
        canvas.width = cropImgElement.width * scaleRatio;
        canvas.height = cropImgElement.height * scaleRatio;

        let cvPoints = null;
        if (typeof cv !== 'undefined' && cv.Mat) {
            try {
                cvPoints = detectDocumentCorners(cropImgElement, canvas.width, canvas.height);
            } catch (e) {
                console.error("OpenCV Processing Error:", e);
            }
        }

        if (cvPoints && cvPoints.length === 4) {
            canvasPts = cvPoints;
        } else {
            const handleOffset = 20;
            const topMarginX = canvas.width * 0.15;
            const topMarginY = canvas.height * 0.10;
            const bottomY = canvas.height - handleOffset;

            canvasPts = [
                {x: topMarginX, y: topMarginY}, 
                {x: canvas.width - topMarginX, y: topMarginY}, 
                {x: canvas.width - handleOffset, y: bottomY}, 
                {x: handleOffset, y: bottomY} 
            ];
        }

        setupCanvasEvents(canvas);
        drawCanvas();
    }, 150); 
}

function closeCropModal() {
    document.getElementById('cropInteractiveModal').classList.add('hidden');
    document.getElementById('cameraInput').value = ""; 
}

function drawCanvas() {
    const canvas = document.getElementById('docCanvas');
    const ctx = canvas.getContext('2d');
    
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(cropImgElement, 0, 0, canvas.width, canvas.height);

    ctx.beginPath();
    ctx.moveTo(canvasPts[0].x, canvasPts[0].y);
    for(let i=1; i<4; i++) {
        ctx.lineTo(canvasPts[i].x, canvasPts[i].y);
    }
    ctx.closePath();
    ctx.fillStyle = "rgba(59, 130, 246, 0.2)"; 
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#3b82f6";
    ctx.stroke();

    ctx.fillStyle = "#ffffff";
    canvasPts.forEach(p => {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 10, 0, 2 * Math.PI);
        ctx.fill();
        ctx.stroke();
    });
}

function setupCanvasEvents(canvas) {
    const newCanvas = canvas.cloneNode(true);
    canvas.parentNode.replaceChild(newCanvas, canvas);
    
    function getEventPos(e) {
        const rect = newCanvas.getBoundingClientRect();
        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const clientY = e.touches ? e.touches[0].clientY : e.clientY;
        return {
            x: clientX - rect.left,
            y: clientY - rect.top
        };
    }

    function handleStart(e) {
        e.preventDefault();
        const pos = getEventPos(e);
        dragPtIndex = canvasPts.findIndex(p => Math.hypot(p.x - pos.x, p.y - pos.y) < 25);
    }

    function handleMove(e) {
        if(dragPtIndex !== -1) {
            e.preventDefault();
            const pos = getEventPos(e);
            canvasPts[dragPtIndex].x = Math.max(0, Math.min(newCanvas.width, pos.x));
            canvasPts[dragPtIndex].y = Math.max(0, Math.min(newCanvas.height, pos.y));
            drawCanvas();
        }
    }

    function handleEnd(e) {
        dragPtIndex = -1;
    }

    newCanvas.addEventListener('mousedown', handleStart);
    newCanvas.addEventListener('mousemove', handleMove);
    window.addEventListener('mouseup', handleEnd);
    newCanvas.addEventListener('touchstart', handleStart, {passive: false});
    newCanvas.addEventListener('touchmove', handleMove, {passive: false});
    window.addEventListener('touchend', handleEnd);
}

async function confirmCropAndProcess() {
    closeCropModal();
    const btn = document.getElementById('btnScanOCR');
    const originalContent = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i> กำลังส่งเข้า Gemini AI...';
    btn.classList.add('opacity-75', 'cursor-not-allowed');
    btn.disabled = true;

    const realPts = canvasPts.map(p => ({
        x: p.x / scaleRatio,
        y: p.y / scaleRatio
    }));

    const formData = new FormData();
    formData.append("file", selectedFileForCrop);
    formData.append("points", JSON.stringify(realPts)); 

    try {
        const response = await fetchWithAuth('/upload', { method: 'POST', body: formData });
        const result = await response.json();

        if (result.success) {
            isOcrScanned = true;
            const data = result.data;
            currentOcrFullData = data;
            
            currentDocNumber = data.document_no || "UNKNOWN-DOC";
            currentPlantTicketcode = data.plant_ticketcode || ""; 
            ocrExpectedQty = (data.pallets_returned && data.pallets_returned.quantity_actual) ? parseInt(data.pallets_returned.quantity_actual) || 0 : 0;

            document.getElementById('ocrBeforeScan').classList.add('hidden');
            document.getElementById('ocrAfterScan').classList.remove('hidden');
            document.getElementById('ocrStatusBadge').classList.remove('hidden');
            
            if (result.images) {
                currentImageBase64 = result.images.full || "";
                currentCropImageBase64 = result.images.crop || "";
                
                document.getElementById('previewImage').src = currentImageBase64;
                
                if (currentCropImageBase64) {
                    document.getElementById('cropImageContainer').classList.remove('hidden');
                    document.getElementById('previewCropImage').src = currentCropImageBase64;
                }
            } else {
                const reader = new FileReader();
                reader.onload = function(e) {
                    currentImageBase64 = e.target.result;
                    document.getElementById('previewImage').src = currentImageBase64;
                };
                reader.readAsDataURL(selectedFileForCrop);
            }

            document.getElementById('docNumberText').innerHTML = currentDocNumber + (currentPlantTicketcode ? ` <span class="ml-2 text-xs bg-purple-100 text-purple-700 px-2 py-1 rounded">โรงงาน: ${currentPlantTicketcode}</span>` : '');
            document.getElementById('expectedTotal').innerText = ocrExpectedQty;
            if (currentPlantTicketcode) {
                try {
                    const res = await fetchWithAuth(`/api/pallets/${encodeURIComponent(currentPlantTicketcode)}`);
                    const palletData = await res.json();
                    if (palletData.success && palletData.pallets.length > 0) {
                        allPallets = palletData.pallets;
                    } else {
                        allPallets = [];
                        console.warn("ไม่พบพาเลทที่ผูกกับโรงงานนี้ใน Master Data");
                    }
                } catch(e) {
                    if (e.message !== "Unauthorized") console.error("ดึงข้อมูลพาเลทไม่สำเร็จ:", e);
                }
            } else {
                allPallets = [];
            }
            
            const trigger = document.getElementById('customSelectTrigger');
            trigger.classList.remove('bg-gray-100', 'text-gray-400', 'cursor-not-allowed');
            trigger.classList.add('bg-white', 'text-gray-800', 'cursor-pointer', 'hover:bg-gray-50');
            document.getElementById('customSelectText').innerText = "-- ค้นหาและเลือกประเภทพาเลท --";
            
            const btnAdd = document.getElementById('btnAdd');
            btnAdd.disabled = false;
            btnAdd.className = "bg-blue-600 hover:bg-blue-700 text-white px-6 py-4 rounded-lg font-bold text-lg flex items-center justify-center transition-all active:scale-95 shadow-sm";

            document.getElementById('palletList').innerHTML = `<div id="emptyState" class="p-8 text-center text-gray-400" style="display: none;"><i class="fa-solid fa-clipboard-list text-4xl mb-3 text-gray-200"></i><p class="text-sm">รอถ่ายรูปใบนำส่ง และเพิ่มประเภทพาเลท</p></div>`;
            palletCount = 0;

            let mfg = (data.pallets_returned && data.pallets_returned.manufacturer) ? data.pallets_returned.manufacturer : "";
            let code = (data.pallets_returned && data.pallets_returned.code) ? data.pallets_returned.code : "";

            if (mfg || code) {
                let defaultPalletName = mfg && code ? `${mfg} - ${code}` : (mfg || code || "พาเลททั่วไป");
                let defaultQty = ocrExpectedQty > 0 ? ocrExpectedQty : 1;
                palletCount = 1;
                document.getElementById('totalItems').innerText = `${palletCount} รายการ`;
                
                const itemId = `pallet-item-${Date.now()}-${Math.random().toString(36).substr(2, 5)}`;
                const html = `
                    <div id="${itemId}" data-name="${defaultPalletName}" class="p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-4 bg-green-50 animate-fade-in border-l-4 border-green-500">
                        <div class="flex-1">
                            <span class="text-xs font-bold text-green-600 mb-1 block"><i class="fa-solid fa-wand-magic-sparkles mr-1"></i> ดึงข้อมูลอัตโนมัติจากใบนำส่ง</span>
                            <h3 class="font-bold text-lg text-blue-800 pallet-name">${defaultPalletName}</h3>
                        </div>
                        <div class="flex items-center gap-3">
                            <div class="flex items-center border border-gray-300 rounded-lg overflow-hidden h-12 bg-white shadow-sm">
                                <button onclick="updateQty('${itemId}', -1)" class="px-4 py-2 bg-gray-100 font-bold border-r h-full active:bg-gray-300"><i class="fa-solid fa-minus"></i></button>
                                <input type="number" id="qty-${itemId}" value="${defaultQty}" min="1" class="qty-input w-20 text-center font-bold text-xl h-full outline-none text-green-700">
                                <button onclick="updateQty('${itemId}', 1)" class="px-4 py-2 bg-gray-100 font-bold border-l h-full active:bg-gray-300"><i class="fa-solid fa-plus"></i></button>
                            </div>
                            <button onclick="removeItem('${itemId}')" class="text-red-500 hover:text-red-700 p-3 bg-white border border-red-100 rounded-lg shadow-sm"><i class="fa-solid fa-trash-can text-xl"></i></button>
                        </div>
                    </div>`;
                document.getElementById('palletList').insertAdjacentHTML('beforeend', html);
            } else {
                document.getElementById('emptyState').style.display = 'block';
                document.getElementById('totalItems').innerText = `0 รายการ`;
            }
        } else {
            showCustomAlert('warning', 'OCR ไม่สำเร็จ', result.message);
            executeRetakeImage();
        }
    } catch (error) {
        if (error.message === "Unauthorized") return;
        console.error(error);
        showCustomAlert('warning', 'ผิดพลาด', 'เชื่อมต่อเซิร์ฟเวอร์ไม่สำเร็จ');
        executeRetakeImage();
    } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-75', 'cursor-not-allowed');
        btn.innerHTML = originalContent;
    }
}

function retakeImage() {
    if (palletCount > 0) {
        document.getElementById('retakeConfirmModal').classList.remove('hidden');
    } else {
        executeRetakeImage();
    }
}

function closeRetakeConfirm() {
    document.getElementById('retakeConfirmModal').classList.add('hidden');
}

function executeRetakeImage() {
    document.getElementById('retakeConfirmModal').classList.add('hidden');
    
    isOcrScanned = false; 
    ocrExpectedQty = 0; 
    currentDocNumber = ""; 
    currentImageBase64 = "";
    currentCropImageBase64 = "";
    currentOcrFullData = {};
    currentPlantTicketcode = "";
    allPallets = []; 

    document.getElementById('cameraInput').value = "";
    document.getElementById('ocrBeforeScan').classList.remove('hidden');
    document.getElementById('ocrAfterScan').classList.add('hidden');
    document.getElementById('ocrStatusBadge').classList.add('hidden');
    document.getElementById('cropImageContainer').classList.add('hidden'); 
    document.getElementById('previewImage').src = "";
    document.getElementById('previewCropImage').src = "";
    document.getElementById('btnScanOCR').innerHTML = '<i class="fa-solid fa-camera mr-2"></i> ถ่ายรูป / สแกนใบนำส่ง';
    document.getElementById('btnScanOCR').classList.remove('opacity-75', 'cursor-not-allowed');
    document.getElementById('btnScanOCR').disabled = false;
    
    const trigger = document.getElementById('customSelectTrigger');
    if(trigger) {
        trigger.classList.add('bg-gray-100', 'text-gray-400', 'cursor-not-allowed');
        trigger.classList.remove('bg-white', 'text-gray-800', 'cursor-pointer', 'hover:bg-gray-50');
    }
    const triggerText = document.getElementById('customSelectText');
    if(triggerText) {
        triggerText.innerText = "-- กรุณาถ่ายรูปใบนำส่งก่อน --";
        triggerText.classList.remove('text-blue-700', 'font-semibold');
    }
    selectedPalletValue = "";
    const menu = document.getElementById('customSelectMenu');
    if (menu) menu.classList.add('hidden');
    const icon = document.getElementById('customSelectIcon');
    if(icon) icon.classList.remove('rotate-180');

    const btnAdd = document.getElementById('btnAdd');
    if (btnAdd) {
        btnAdd.disabled = true; 
        btnAdd.className = "bg-gray-400 text-white px-6 py-4 rounded-lg font-bold text-lg flex items-center justify-center cursor-not-allowed";
    }

    palletCount = 0;
    document.getElementById('palletList').innerHTML = `<div id="emptyState" class="p-8 text-center text-gray-400"><i class="fa-solid fa-clipboard-list text-4xl mb-3 text-gray-200"></i><p class="text-sm">รอถ่ายรูปใบนำส่ง และเพิ่มประเภทพาเลท</p></div>`;
    document.getElementById('totalItems').innerText = `0 รายการ`;
}

function toggleDropdown() {
    if(!isOcrScanned) return; 
    const menu = document.getElementById('customSelectMenu');
    const icon = document.getElementById('customSelectIcon');
    
    if (menu.classList.contains('hidden')) {
        menu.classList.remove('hidden');
        icon.classList.add('rotate-180');
        document.getElementById('palletSearchInput').value = ""; 
        renderDropdownList();
        document.getElementById('palletSearchInput').focus(); 
    } else {
        menu.classList.add('hidden');
        icon.classList.remove('rotate-180');
    }
}

function renderDropdownList() {
    const searchTerm = document.getElementById('palletSearchInput').value.toLowerCase();
    const listContainer = document.getElementById('palletOptionsList');
    listContainer.innerHTML = ''; 

    const filteredAll = allPallets.filter(p => p.toLowerCase().includes(searchTerm));

    if (filteredAll.length === 0) {
        listContainer.innerHTML = `<li class="p-4 text-center text-gray-500 text-sm">ไม่พบพาเลทที่ค้นหา หรือโรงงานนี้ไม่มีพาเลท</li>`;
        return;
    }

    listContainer.innerHTML += `<li class="px-4 py-2 bg-blue-50 text-xs font-bold text-blue-700 uppercase tracking-wider sticky top-0 shadow-sm"><i class="fa-solid fa-boxes-stacked mr-1"></i> ประเภทพาเลทอ้างอิงตามโรงงาน</li>`;
    
    filteredAll.forEach(pallet => {
        listContainer.innerHTML += `<li class="px-4 py-3 hover:bg-blue-100 cursor-pointer text-gray-800 border-b border-gray-100 transition-colors" onclick="selectPalletOption('${pallet}')">${pallet}</li>`;
    });
}

function selectPalletOption(palletName) {
    selectedPalletValue = palletName;
    document.getElementById('customSelectText').innerText = palletName;
    document.getElementById('customSelectText').classList.add('text-blue-700', 'font-semibold');
    document.getElementById('customSelectMenu').classList.add('hidden');
    document.getElementById('customSelectIcon').classList.remove('rotate-180');
}

function addPallet() {
    if(!isOcrScanned) return;
    if (!selectedPalletValue) { showCustomAlert('warning', 'แจ้งเตือน', 'กรุณาเลือกประเภทพาเลทก่อนครับ'); return; }
    if (document.querySelectorAll(`[data-name="${selectedPalletValue}"]`).length > 0) {
        showCustomAlert('warning', 'ข้อมูลซ้ำ', 'พาเลทประเภทนี้ถูกเพิ่มแล้วครับ'); 
        resetCustomSelect();
        return;
    }

    document.getElementById('emptyState').style.display = 'none';
    palletCount++;
    document.getElementById('totalItems').innerText = `${palletCount} รายการ`;
    
    const itemId = `pallet-item-${Date.now()}-${Math.random().toString(36).substr(2, 5)}`;
    
    const html = `
        <div id="${itemId}" data-name="${selectedPalletValue}" class="p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div class="flex-1"><h3 class="font-bold text-lg text-blue-800 pallet-name">${selectedPalletValue}</h3></div>
            <div class="flex items-center gap-3">
                <div class="flex items-center border border-gray-300 rounded-lg overflow-hidden h-12 bg-white">
                    <button onclick="updateQty('${itemId}', -1)" class="px-4 py-2 bg-gray-100 font-bold border-r h-full active:bg-gray-300"><i class="fa-solid fa-minus"></i></button>
                    <input type="number" id="qty-${itemId}" value="10" min="1" class="qty-input w-20 text-center font-bold text-xl h-full outline-none">
                    <button onclick="updateQty('${itemId}', 1)" class="px-4 py-2 bg-gray-100 font-bold border-l h-full active:bg-gray-300"><i class="fa-solid fa-plus"></i></button>
                </div>
                <button onclick="removeItem('${itemId}')" class="text-red-500 hover:text-red-700 p-3 bg-red-50 rounded-lg"><i class="fa-solid fa-trash-can text-xl"></i></button>
            </div>
        </div>`;
    document.getElementById('palletList').insertAdjacentHTML('beforeend', html);
    resetCustomSelect();
}

function resetCustomSelect() {
    selectedPalletValue = "";
    const triggerText = document.getElementById('customSelectText');
    if(triggerText) {
        triggerText.innerText = "-- ค้นหาและเลือกประเภทพาเลท --";
        triggerText.classList.remove('text-blue-700', 'font-semibold');
    }
}

function updateQty(itemId, change) {
    const input = document.getElementById(`qty-${itemId}`);
    let val = parseInt(input.value) || 0;
    if (val + change >= 1) {
        input.value = val + change;
        input.classList.remove('flash-green'); void input.offsetWidth; input.classList.add('flash-green');
    }
}

function removeItem(itemId) {
    document.getElementById(itemId).remove(); palletCount--;
    document.getElementById('totalItems').innerText = `${palletCount} รายการ`;
    if (palletCount === 0) document.getElementById('emptyState').style.display = 'block';
}

function previewData() {
    if (!isOcrScanned || palletCount === 0) {
        showCustomAlert('warning', 'เตือน', 'กรุณาสแกนใบนำส่งและกรอกข้อมูลก่อนครับ'); return;
    }
    
    const summaryList = document.getElementById('summaryList');
    summaryList.innerHTML = ''; totalActualPiecesGlobal = 0;

    document.querySelectorAll('#palletList > div[id^="pallet-item-"]').forEach(item => {
        const name = item.querySelector('.pallet-name').innerText;
        const qty = parseInt(item.querySelector('.qty-input').value) || 0;
        totalActualPiecesGlobal += qty;
        summaryList.innerHTML += `<div class="flex justify-between py-2 items-center"><span class="text-gray-700">${name}</span><span class="font-bold text-blue-700">${qty}</span></div>`;
    });

    const warnBox = document.getElementById('discrepancyWarning');
    let colorClass = totalActualPiecesGlobal !== ocrExpectedQty ? "text-red-600" : "text-green-600";
    totalActualPiecesGlobal !== ocrExpectedQty ? warnBox.classList.remove('hidden') : warnBox.classList.add('hidden');

    summaryList.innerHTML += `
        <div class="flex justify-between py-3 mt-2 border-t bg-gray-100 px-2 rounded"><span class="font-bold">จำนวนที่ลูกค้าเขียน</span><span class="font-bold text-gray-700">${ocrExpectedQty}</span></div>
        <div class="flex justify-between py-3 border-t px-2"><span class="font-bold">จำนวนที่ Checker นับได้</span><span class="font-bold ${colorClass} text-xl">${totalActualPiecesGlobal}</span></div>`;
    
    const modalTitle = document.getElementById('confirmModalTitle');
    if (editingRecordId) {
        modalTitle.innerHTML = `<i class="fa-solid fa-pen-to-square mr-2"></i>ยืนยันการแก้ไขรายการ`;
    } else {
        modalTitle.innerHTML = `<i class="fa-solid fa-list-check mr-2"></i>สรุปรายการก่อนบันทึก`;
    }

    document.getElementById('confirmModal').classList.remove('hidden');
}

function closeModal() { document.getElementById('confirmModal').classList.add('hidden'); }

async function confirmSubmit() {
    const btn = document.getElementById('btnConfirmSubmit');
    const originalContent = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add('opacity-75', 'cursor-not-allowed');
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i> กำลังบันทึกข้อมูล...';

    const pallets = [];
    document.querySelectorAll('#palletList > div[id^="pallet-item-"]').forEach(item => {
        const name = item.querySelector('.pallet-name').innerText;
        const qty = parseInt(item.querySelector('.qty-input').value) || 0;
        pallets.push({ name, qty });
    });

    const now = new Date();
    const dateStr = `${String(now.getDate()).padStart(2, '0')}/${String(now.getMonth() + 1).padStart(2, '0')}/${now.getFullYear()}`;
    
    const payload = {
        id: editingRecordId ? editingRecordId : null,
        documentNumber: currentDocNumber,
        date: dateStr,
        customer_name: (currentOcrFullData.customer && currentOcrFullData.customer.name) ? currentOcrFullData.customer.name : "ไม่ระบุชื่อ",
        customer_code: (currentOcrFullData.customer && currentOcrFullData.customer.code) ? currentOcrFullData.customer.code : "N/A",
        expectedQty: ocrExpectedQty,
        actualQty: totalActualPiecesGlobal,
        checkerName: currentUser,
        palletDetails: pallets,
        imageBase64: currentImageBase64,
        plant_ticketcode: currentPlantTicketcode
    };

    try {
        const response = await fetchWithAuth('/api/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const resData = await response.json();
        
        if (resData.success) {
            closeModal();
            showCustomAlert('success', editingRecordId ? 'แก้ไขสำเร็จ!' : 'บันทึกสำเร็จ!', resData.message);
            resetCheckerApp();
        } else {
            showCustomAlert('warning', 'บันทึกไม่สำเร็จ', resData.message);
        }
    } catch (error) {
        if (error.message === "Unauthorized") return;
        console.error(error);
        showCustomAlert('warning', 'การเชื่อมต่อผิดพลาด', 'ไม่สามารถเชื่อมต่อระบบฐานข้อมูลได้');
    } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-75', 'cursor-not-allowed');
        btn.innerHTML = originalContent;
    }
}

function resetCheckerApp() {
    editingRecordId = null; 
    currentOcrFullData = {};
    currentPlantTicketcode = "";
    const editBanner = document.getElementById('editModeBanner');
    if (editBanner) editBanner.classList.add('hidden'); 

    const btnSubmit = document.getElementById('btnSubmitData');
    if(btnSubmit) {
        btnSubmit.innerHTML = `<i class="fa-solid fa-check-circle mr-2"></i> ตรวจสอบและบันทึก`;
        btnSubmit.classList.replace('bg-yellow-500', 'bg-green-600');
        btnSubmit.classList.replace('hover:bg-yellow-600', 'hover:bg-green-700');
    }

    executeRetakeImage(); 
}

async function editRecord(recordId) {
    try {
        const response = await fetchWithAuth('/api/records');
        const result = await response.json();
        if(!result.success) return;

        const record = result.data.find(r => r.id === recordId);
        if(!record) return;

        editingRecordId = recordId;

        document.getElementById('checkerHistoryPage').classList.add('hidden');
        document.getElementById('checkerPage').classList.remove('hidden');

        document.getElementById('editModeBanner').classList.remove('hidden');
        document.getElementById('editModeDocNumber').innerText = record.documentNumber;
        
        document.getElementById('btnSubmitData').innerHTML = `<i class="fa-solid fa-floppy-disk mr-2"></i> บันทึกการแก้ไข`;
        document.getElementById('btnSubmitData').classList.replace('bg-green-600', 'bg-yellow-500');
        document.getElementById('btnSubmitData').classList.replace('hover:bg-green-700', 'hover:bg-yellow-600');

        isOcrScanned = true;
        currentDocNumber = record.documentNumber;
        ocrExpectedQty = record.expectedQty;
        currentImageBase64 = record.imageBase64;
        currentCropImageBase64 = "";
        currentPlantTicketcode = record.plant_ticketcode || ""; 
        
        document.getElementById('ocrBeforeScan').classList.add('hidden');
        document.getElementById('ocrAfterScan').classList.remove('hidden');
        document.getElementById('ocrStatusBadge').classList.remove('hidden');
        document.getElementById('previewImage').src = currentImageBase64 || 'https://via.placeholder.com/150x200?text=No+Image';
        document.getElementById('cropImageContainer').classList.add('hidden');
        
        let displayDocText = currentDocNumber;
        if (currentPlantTicketcode) {
            displayDocText += ` <span class="ml-2 text-xs bg-purple-100 text-purple-700 px-2 py-1 rounded border border-purple-300 shadow-sm"><i class="fa-solid fa-industry mr-1"></i>โรงงาน: ${currentPlantTicketcode}</span>`
        }
        document.getElementById('docNumberText').innerHTML = displayDocText;
        document.getElementById('expectedTotal').innerText = ocrExpectedQty;

        const trigger = document.getElementById('customSelectTrigger');
        trigger.classList.remove('bg-gray-100', 'text-gray-400', 'cursor-not-allowed');
        trigger.classList.add('bg-white', 'text-gray-800', 'cursor-pointer', 'hover:bg-gray-50');
        document.getElementById('customSelectText').innerText = "-- ค้นหาและเลือกประเภทพาเลท --";
        document.getElementById('btnAdd').disabled = false;
        document.getElementById('btnAdd').className = "bg-blue-600 hover:bg-blue-700 text-white px-6 py-4 rounded-lg font-bold text-lg flex items-center justify-center transition-all active:scale-95 shadow-sm";

        palletCount = 0;
        document.getElementById('emptyState').style.display = 'none';
        document.getElementById('palletList').innerHTML = ''; 

        if(record.palletDetails && record.palletDetails.length > 0) {
            record.palletDetails.forEach(p => {
                palletCount++;
                const itemId = `pallet-item-${Date.now()}-${Math.random().toString(36).substr(2, 5)}`;
                const html = `
                    <div id="${itemId}" data-name="${p.name}" class="p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                        <div class="flex-1"><h3 class="font-bold text-lg text-blue-800 pallet-name">${p.name}</h3></div>
                        <div class="flex items-center gap-3">
                            <div class="flex items-center border border-gray-300 rounded-lg overflow-hidden h-12 bg-white">
                                <button onclick="updateQty('${itemId}', -1)" class="px-4 py-2 bg-gray-100 font-bold border-r h-full active:bg-gray-300"><i class="fa-solid fa-minus"></i></button>
                                <input type="number" id="qty-${itemId}" value="${p.qty}" min="1" class="qty-input w-20 text-center font-bold text-xl h-full outline-none">
                                <button onclick="updateQty('${itemId}', 1)" class="px-4 py-2 bg-gray-100 font-bold border-l h-full active:bg-gray-300"><i class="fa-solid fa-plus"></i></button>
                            </div>
                            <button onclick="removeItem('${itemId}')" class="text-red-500 hover:text-red-700 p-3 bg-red-50 rounded-lg"><i class="fa-solid fa-trash-can text-xl"></i></button>
                        </div>
                    </div>`;
                document.getElementById('palletList').insertAdjacentHTML('beforeend', html);
            });
        }
        document.getElementById('totalItems').innerText = `${palletCount} รายการ`;
        window.scrollTo({ top: 0, behavior: 'smooth' });

        if (currentPlantTicketcode) {
            try {
                const res = await fetchWithAuth(`/api/pallets/${currentPlantTicketcode}`);
                const palletData = await res.json();
                if (palletData.success && palletData.pallets.length > 0) {
                    allPallets = palletData.pallets;
                } else {
                    allPallets = [];
                }
            } catch(e) {
                if (e.message !== "Unauthorized") console.error(e);
            }
        }

    } catch (error) {
        if (error.message === "Unauthorized") return;
        console.error(error);
        showCustomAlert('warning', 'ผิดพลาด', 'ดึงข้อมูลไม่สำเร็จ');
    }
}

function openCheckerHistoryPage() {
    if(editingRecordId) resetCheckerApp();
    const btnSubmitData = document.getElementById('btnSubmitData');
    if (btnSubmitData) {
        btnSubmitData.innerHTML = `<i class="fa-solid fa-check-circle mr-2"></i> ตรวจสอบและบันทึก`;
        btnSubmitData.classList.replace('bg-yellow-500', 'bg-green-600');
        btnSubmitData.classList.replace('hover:bg-yellow-600', 'hover:bg-green-700');
    }
    document.getElementById('checkerPage').classList.add('hidden');
    document.getElementById('checkerHistoryPage').classList.remove('hidden');
    
    const now = new Date();
    const todayISO = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    document.getElementById('checkerDateFilter').value = todayISO;
    renderCheckerHistory();
}

function closeCheckerHistoryPage() {
    document.getElementById('checkerHistoryPage').classList.add('hidden');
    document.getElementById('checkerPage').classList.remove('hidden');
}

function clearCheckerDateFilter() {
    document.getElementById('checkerDateFilter').value = "";
    renderCheckerHistory();
}

async function renderCheckerHistory() {
    try {
        const response = await fetchWithAuth('/api/records');
        const result = await response.json();
        if(!result.success) return;

        const db = result.data;
        const filterDate = document.getElementById('checkerDateFilter').value; 
        
        let myRecords = db.filter(r => r.checkerName === currentUser);

        if (filterDate) {
            const [year, month, day] = filterDate.split('-');
            const formattedFilter = `${day}/${month}/${year}`;
            myRecords = myRecords.filter(r => r.date === formattedFilter);
        }

        const container = document.getElementById('checkerHistoryContainerList');
        if(myRecords.length === 0) {
            container.innerHTML = `
                <div class="text-center text-gray-400 py-16 bg-white rounded-xl shadow-sm border border-gray-200">
                    <i class="fa-solid fa-folder-open text-5xl mb-3 text-gray-300"></i>
                    <p class="text-lg">ไม่พบประวัติการทำรายการ</p>
                </div>`;
            return;
        }

        let html = '';
        myRecords.forEach(record => {
            const isError = record.expectedQty !== record.actualQty;
            const bgClass = isError ? 'card-bg-light border-red-200' : 'bg-white border-gray-200';
            const statusBadge = isError 
                ? `<span class="bg-red-100 text-red-600 px-3 py-1 rounded-full text-xs font-bold shadow-sm">ยอดไม่ตรง</span>` 
                : `<span class="bg-green-100 text-green-700 px-3 py-1 rounded-full text-xs font-bold shadow-sm">ยอดตรง</span>`;
            
            let palletDetailsHtml = '';
            if(record.palletDetails && record.palletDetails.length > 0) {
                palletDetailsHtml = '<ul class="mt-3 space-y-1.5 border-t border-gray-200 pt-3 bg-gray-50 p-3 rounded-lg">';
                record.palletDetails.forEach(p => {
                    palletDetailsHtml += `
                        <li class="text-sm flex justify-between text-gray-700 items-center font-medium">
                            <span><i class="fa-solid fa-boxes-stacked text-blue-500 mr-2"></i>${p.name}</span>
                            <span class="bg-blue-100 text-blue-800 px-2 py-0.5 rounded font-bold">${p.qty} ตัว</span>
                        </li>`;
                });
                palletDetailsHtml += '</ul>';
            }

            const editActionHtml = `<button onclick="editRecord(${record.id})" class="mt-4 w-full bg-yellow-500 hover:bg-yellow-600 text-white font-bold py-2.5 rounded-lg transition-all active:scale-95 shadow-sm"><i class="fa-solid fa-pen-to-square mr-2"></i>แก้ไขข้อมูล</button>`;

            html += `
                <div class="${bgClass} rounded-xl p-5 shadow-sm border flex flex-col sm:flex-row gap-5 transition-transform hover:-translate-y-1">
                    <div class="relative flex-shrink-0 cursor-pointer group sm:w-40" onclick="openImageModal('${record.imageBase64 || ''}')">
                        <img src="${record.imageBase64 || 'https://via.placeholder.com/150x200?text=No+Image'}" class="w-full h-40 sm:h-full object-cover rounded-lg border border-gray-300 shadow-sm group-hover:brightness-90 transition-all">
                        <div class="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                            <i class="fa-solid fa-magnifying-glass-plus text-white text-2xl drop-shadow-md"></i>
                        </div>
                    </div>
                    <div class="flex-1 flex flex-col justify-between">
                        <div>
                            <div class="flex justify-between items-start mb-2">
                                <h3 class="text-2xl font-extrabold text-blue-900 tracking-tight">${record.documentNumber}</h3>
                                ${statusBadge}
                            </div>
                            <p class="text-sm text-gray-500 mb-3"><i class="fa-regular fa-calendar mr-1"></i> ${record.date} 
                                ${record.plant_ticketcode ? `<span class="ml-2 text-xs bg-purple-100 text-purple-700 px-2 py-1 rounded font-bold">อ้างอิง: ${record.plant_ticketcode}</span>` : ''}
                            </p>
                            
                            <div class="grid grid-cols-2 gap-3 text-sm bg-white p-3 rounded-lg border border-gray-200">
                                <div class="text-gray-500">ลูกค้าเขียน: <span class="font-bold text-lg text-gray-800 block">${record.expectedQty}</span></div>
                                <div class="text-gray-500">นับได้จริง: <span class="font-bold text-lg block ${isError ? 'text-red-600' : 'text-green-600'}">${record.actualQty}</span></div>
                            </div>
                            
                            ${palletDetailsHtml}
                        </div>
                        ${editActionHtml}
                    </div>
                </div>`;
        });
        container.innerHTML = html;
    } catch (error) {
        if (error.message === "Unauthorized") return;
        console.error("โหลดประวัติไม่สำเร็จ", error);
    }
}

function initAdminDashboard() {
    if(!sessionStorage.getItem("access_token")) return;

    updateAdminDashboard(); 
    generateMonthlyReport();
    
    if (!adminWs || adminWs.readyState === WebSocket.CLOSED) {
        const protocol = window.location.protocol === "https:" ? "wss" : "ws";
        adminWs = new WebSocket(`${protocol}://${window.location.host}/ws`);
        
        adminWs.onmessage = async function(event) {
            if (event.data === "UPDATE") {
                if(sessionStorage.getItem("access_token")){
                    console.log("ได้รับสัญญาณอัปเดตจากเซิร์ฟเวอร์! ดึงข้อมูลล่าสุด...");
                    await updateAdminDashboard();
                    generateMonthlyReport();
                    renderScaleDashboard();
                }
            }
        };

        adminWs.onclose = function() {
            if(sessionStorage.getItem("access_token")){
                console.log("WebSocket ตัดการเชื่อมต่อ... จะลองต่อใหม่ใน 5 วินาที");
                setTimeout(initAdminDashboard, 3000);
            }
            else {
                console.log("ผู้ใช้ออกจากระบบ ปิด WebSocket ถาวร")
            }
        };
    }
}

function switchScaleTab(tabName) {
    const btnDaily = document.getElementById('btnScaleTabDaily');
    const btnMonthly = document.getElementById('btnScaleTabMonthly');
    const btnScale = document.getElementById('btnScaleTabScale');

    const viewDaily = document.getElementById('divScaleDailyView');
    const viewMonthly = document.getElementById('divScaleMonthlyView');
    const viewScale = document.getElementById('divScaleScaleView');

    [btnDaily, btnMonthly, btnScale].forEach(btn => {
        if(btn) btn.className = "px-6 py-3 font-bold text-gray-500 hover:text-gray-700 border-b-4 border-transparent transition-colors";
    });
    [viewDaily, viewMonthly, viewScale].forEach(view => {
        if(view) view.classList.add('hidden');
    });

    if (tabName === 'daily') {
        if(btnDaily) btnDaily.className = "px-6 py-3 font-bold text-teal-600 border-b-4 border-teal-600 transition-colors";
        if(viewDaily) viewDaily.classList.remove('hidden');
        renderAdminDashboard(); 
    } else if (tabName === 'monthly') {
        if(btnMonthly) btnMonthly.className = "px-6 py-3 font-bold text-teal-600 border-b-4 border-teal-600 transition-colors";
        if(viewMonthly) viewMonthly.classList.remove('hidden');
        generateMonthlyReport();
    } else if (tabName === 'scale') {
        if(btnScale) btnScale.className = "px-6 py-3 font-bold text-teal-600 border-b-4 border-teal-600 transition-colors";
        if(viewScale) viewScale.classList.remove('hidden');
        renderScaleDashboard();
    }
}

function switchAdminTab(tabName) {
    const btnDaily = document.getElementById('tabDaily');
    const btnMonthly = document.getElementById('tabMonthly');
    const btnScale = document.getElementById('tabScale');
    const btnUsers = document.getElementById('tabUsers');

    const viewDaily = document.getElementById('adminDailyView');
    const viewMonthly = document.getElementById('adminMonthlyView');
    const viewScale = document.getElementById('adminScaleView');
    const viewUsers = document.getElementById('adminUsersView');

    [btnDaily, btnMonthly, btnScale, btnUsers].forEach(btn => {
        if(btn) btn.className = "px-6 py-3 font-bold text-gray-500 hover:text-gray-700 border-b-4 border-transparent transition-colors";
    });
    [viewDaily, viewMonthly, viewScale, viewUsers].forEach(view => {
        if(view) view.classList.add('hidden');
    });

    if (tabName === 'daily') {
        btnDaily.className = "px-6 py-3 font-bold text-blue-600 border-b-4 border-blue-600 transition-colors";
        viewDaily.classList.remove('hidden');
        renderAdminDashboard(); 
    } else if (tabName === 'monthly') {
        btnMonthly.className = "px-6 py-3 font-bold text-blue-600 border-b-4 border-blue-600 transition-colors";
        viewMonthly.classList.remove('hidden');
        generateMonthlyReport();
    } else if (tabName === 'scale') {
        btnScale.className = "px-6 py-3 font-bold text-teal-600 border-b-4 border-teal-600 transition-colors";
        viewScale.classList.remove('hidden');
        renderScaleDashboard();
    } else if (tabName === 'users') {
        btnUsers.className = "px-6 py-3 font-bold text-purple-600 border-b-4 border-purple-600 transition-colors";
        viewUsers.classList.remove('hidden');
        switchUserTab('pending');
    }
}


async function approveUser(userId) {
    // 1. เปลี่ยน UI ทันทีเพื่อให้ Feedback กับแอดมิน
    const actionContainer = document.getElementById(`action-container-${userId}`);
    const originalHtml = actionContainer.innerHTML;
    actionContainer.innerHTML = `<span class="bg-green-100 text-green-700 px-3 py-1.5 rounded-lg font-bold text-sm inline-flex items-center"><i class="fa-solid fa-spinner fa-spin mr-2"></i> กำลังประมวลผล...</span>`;

    try {
        const response = await fetchWithAuth(`/auth/users/approve/${userId}`, { method: 'PUT' });
        const result = await response.json();

        if(result.success) {
            // 2. แสดงสถานะว่า "อนุมัติแล้ว" ชัดเจน
            actionContainer.innerHTML = `<span class="bg-green-500 text-white px-3 py-1.5 rounded-lg font-bold text-sm inline-flex items-center"><i class="fa-solid fa-check-circle mr-2"></i> อนุมัติแล้ว</span>`;
            
            showCustomAlert('success', 'อนุมัติสำเร็จ', 'ผู้ใช้งานสามารถเข้าสู่ระบบได้แล้ว');
            
            // 3. หน่วงเวลา 1.5 วินาทีให้แอดมินเห็นสถานะ ก่อนรีเฟรชตาราง
            setTimeout(() => {
                loadPendingUsers(); 
            }, 1500);
        } else {
            actionContainer.innerHTML = originalHtml;
            showCustomAlert('warning', 'เกิดข้อผิดพลาด', result.detail || 'ไม่สามารถอนุมัติได้');
        }
    } catch(e) {
        actionContainer.innerHTML = originalHtml;
        if(e.message !== "Unauthorized") console.error("อนุมัติไม่สำเร็จ", e);
    }
}

async function updateAdminDashboard(page=1) {
    const limit = 50;
    const offset = (page-1)*limit;
    const tbody = document.getElementById('adminRecordTableBody');

    // skeleton loader
    tbody.innerHTML = Array(5).fill().map(() => `
        <tr class="animate-pulse bg-white border-b border-gray-100">
            <td class="px-4 py-4"><div class="h-4 bg-gray-200 rounded w-24"></div></td>
            <td class="px-4 py-4"><div class="h-4 bg-gray-200 rounded w-20"></div></td>
            <td class="px-4 py-4"><div class="h-4 bg-gray-200 rounded w-32"></div></td>
            <td class="px-4 py-4"><div class="h-4 bg-gray-200 rounded w-16"></div></td>
            <td colspan="6"></td>
        </tr>
    `).join('');

    try {
        const response = await fetchWithAuth(`/api/records?limit=${limit}&offset=${offset}`);
        const result = await response.json();
        
        if (!result.success) return;
        
        globalAdminData = result.data; 
        renderAdminDashboard(); 
        renderScaleDashboard();
    } catch (error) {
        if (error.message === "Unauthorized") return;
        console.error("ไม่สามารถโหลดข้อมูล Admin Dashboard ได้:", error);
    }
}

function toggleDiscrepancyFilter() {
    isDailyDiscrepancyFilterOn = true;
    renderAdminDashboard(); 
}

function clearDiscrepancyFilter() {
    isDailyDiscrepancyFilterOn = false;
    renderAdminDashboard(); 
}

function toggleScaleDiscrepancyFilter() {
    isDailyDiscrepancyFilterOn = true;
    renderAdminDashboard(); 
}

function clearScaleDiscrepancyFilter() {
    isDailyDiscrepancyFilterOn = false;
    renderAdminDashboard(); 
}

function renderAdminDashboard() {
    if (!globalAdminData) return;
    let db = globalAdminData; 
    
    let targetDateStr = "";
    const isAdminVisible = !document.getElementById('adminPage').classList.contains('hidden');
    const activeDatePicker = isAdminVisible ? document.getElementById('adminDatePicker') : document.getElementById('scaleDatePicker');
    
    if (activeDatePicker && activeDatePicker.value) {
        const [year, month, day] = activeDatePicker.value.split('-');
        targetDateStr = `${day}/${month}/${year}`;
    } else {
        const now = new Date();
        targetDateStr = `${String(now.getDate()).padStart(2, '0')}/${String(now.getMonth() + 1).padStart(2, '0')}/${now.getFullYear()}`;
    }
    
    let todayRecords = db.filter(r => r.date && r.date.includes(targetDateStr));
    
    const totalExpected = todayRecords.reduce((sum, r) => sum + r.expectedQty, 0);
    const totalActual = todayRecords.reduce((sum, r) => sum + r.actualQty, 0);
    const totalDiff = totalActual - totalExpected;

    const statExpected = document.getElementById('adminStatExpected');
    if (statExpected) statExpected.innerText = totalExpected;
    const scaleStatExpected = document.getElementById('scaleStatExpected');
    if (scaleStatExpected) scaleStatExpected.innerText = totalExpected;

    const statActual = document.getElementById('adminStatActual');
    if (statActual) statActual.innerText = totalActual;
    const scaleStatActual = document.getElementById('scaleStatActual');
    if (scaleStatActual) scaleStatActual.innerText = totalActual;

    const statDiff = document.getElementById('adminStatDiff');
    if (statDiff) {
        statDiff.innerText = totalDiff > 0 ? `+${totalDiff}` : totalDiff;
        statDiff.className = totalDiff !== 0 ? "text-xl font-bold text-red-600" : "text-xl font-bold text-gray-500";
    }
    const scaleStatDiff = document.getElementById('scaleStatDiff');
    if (scaleStatDiff) {
        scaleStatDiff.innerText = totalDiff > 0 ? `+${totalDiff}` : totalDiff;
        scaleStatDiff.className = totalDiff !== 0 ? "text-xl font-bold text-red-600" : "text-xl font-bold text-gray-500";
    }

    const statDiscrepancy = document.getElementById('adminStatDiscrepancy');
    if (statDiscrepancy) statDiscrepancy.innerText = todayRecords.filter(r => r.expectedQty !== r.actualQty).length;
    const scaleStatDiscrepancy = document.getElementById('scaleStatDiscrepancy');
    if (scaleStatDiscrepancy) scaleStatDiscrepancy.innerText = todayRecords.filter(r => r.expectedQty !== r.actualQty).length;

    const btnClearFilter = document.getElementById('btnClearDailyFilter');
    const scaleBtnClearFilter = document.getElementById('btnScaleClearDailyFilter');
    if (isDailyDiscrepancyFilterOn) {
        todayRecords = todayRecords.filter(r => r.expectedQty !== r.actualQty);
        if (btnClearFilter) btnClearFilter.classList.remove('hidden');
        if (scaleBtnClearFilter) scaleBtnClearFilter.classList.remove('hidden');
    } else {
        if (btnClearFilter) btnClearFilter.classList.add('hidden');
        if (scaleBtnClearFilter) scaleBtnClearFilter.classList.add('hidden');
    }

    const tableBody = document.getElementById('adminRecordTableBody');
    const scaleTableBody = document.getElementById('scaleDailyRecordTableBody');
    if(todayRecords.length === 0) {
        const emptyMsg = `<tr><td colspan="10" class="text-center text-gray-500 py-10 text-lg">${isDailyDiscrepancyFilterOn ? 'ไม่พบบิลที่ยอดไม่ตรง' : `ไม่พบข้อมูลของวันที่ ${targetDateStr}`}</td></tr>`;
        if(tableBody) tableBody.innerHTML = emptyMsg;
        if(scaleTableBody) scaleTableBody.innerHTML = emptyMsg;
        return;
    }

    let html = '';
    todayRecords.forEach(record => {
        const isDiscrepancy = record.expectedQty !== record.actualQty;
        const statusColor = isDiscrepancy ? 'text-red-600 font-bold bg-red-50' : 'text-green-600 font-bold';
        
        const diffValue = record.actualQty - record.expectedQty;
        const diffText = diffValue > 0 ? `+${diffValue}` : diffValue;
        const diffColor = diffValue !== 0 ? 'text-red-600 font-bold' : 'text-gray-400';
        
        let palletItemsHtml = '-';
        if (record.palletDetails && record.palletDetails.length > 0) {
            palletItemsHtml = `
            <table class="w-full text-xs text-left border border-gray-200 rounded overflow-hidden">
                <tbody class="divide-y divide-gray-100">
                    ${record.palletDetails.map(p => `
                        <tr class="bg-gray-50 hover:bg-gray-100">
                            <td class="px-2 py-1 text-gray-700 w-3/4">${p.name}</td>
                            <td class="px-2 py-1 font-bold text-blue-700 text-right w-1/4 border-l border-gray-100">${p.qty}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>`;
        }

        html += `
            <tr class="hover:bg-blue-50 transition-colors border-b border-gray-100">
                <td class="px-4 py-3 font-bold text-gray-800">${record.documentNumber}</td>
                <td class="px-4 py-3 text-sm">${record.date}</td>
                <td class="px-4 py-3 truncate max-w-xs text-sm" title="${record.customer_name}">${record.customer_name}</td>
                <td class="px-4 py-3 text-sm text-purple-700 font-semibold">${record.plant_ticketcode || '-'}</td>
                <td class="px-4 py-3 text-center text-sm">${record.checkerName}</td>
                <td class="px-4 py-3 text-right">${record.expectedQty}</td>
                <td class="px-4 py-3 text-right ${statusColor}">${record.actualQty}</td>
                <td class="px-4 py-3 text-center ${diffColor}">${diffText}</td>
                <td class="px-4 py-3 text-right font-bold text-green-700">${record.calculated_weight ? record.calculated_weight.toLocaleString() : '0'}</td>
                <td class="px-4 py-2">${palletItemsHtml}</td>
                <td class="px-4 py-3 text-center">
                    <button onclick="openImageModal('${record.imageBase64 || ''}')" class="text-blue-500 hover:text-blue-700 bg-blue-100 hover:bg-blue-200 p-2 rounded-lg transition-colors" title="ดูรูปภาพ">
                        <i class="fa-solid fa-image"></i>
                    </button>
                </td>
            </tr>`;
    });
    if(tableBody) tableBody.innerHTML = html;
    if(scaleTableBody) scaleTableBody.innerHTML = html;
}

async function generateMonthlyReport() {
    try {
        const response = await fetchWithAuth('/api/records');
        const result = await response.json();
        
        if (!result.success) return;
        
        const db = result.data;
        const isAdminVisible = !document.getElementById('adminPage').classList.contains('hidden');
        const activeMonthPicker = isAdminVisible ? document.getElementById('reportMonthPicker') : document.getElementById('scaleReportMonthPicker');
        const selectedMonthStr = activeMonthPicker ? activeMonthPicker.value : "";
        
        const monthlyRecords = db.filter(r => {
            if (r.date) {
                const parts = r.date.split('/'); 
                if(parts.length === 3) {
                    return `${parts[2]}-${parts[1]}` === selectedMonthStr;
                }
            }
            return false;
        });

        const totalBills = monthlyRecords.length;
        const totalPallets = monthlyRecords.reduce((sum, r) => sum + r.actualQty, 0);
        const discrepancyBills = monthlyRecords.filter(r => r.expectedQty !== r.actualQty).length;
        
        let palletDiff = 0;
        monthlyRecords.forEach(r => {
            if(r.expectedQty !== r.actualQty) {
                palletDiff += (r.actualQty - r.expectedQty); 
            }
        });

        if(document.getElementById('monthlyTotalBills')) document.getElementById('monthlyTotalBills').innerText = totalBills;
        if(document.getElementById('scaleMonthlyTotalBills')) document.getElementById('scaleMonthlyTotalBills').innerText = totalBills;
        
        if(document.getElementById('monthlyTotalPallets')) document.getElementById('monthlyTotalPallets').innerText = totalPallets;
        if(document.getElementById('scaleMonthlyTotalPallets')) document.getElementById('scaleMonthlyTotalPallets').innerText = totalPallets;

        if(document.getElementById('monthlyDiscrepancyBills')) document.getElementById('monthlyDiscrepancyBills').innerText = discrepancyBills;
        if(document.getElementById('scaleMonthlyDiscrepancyBills')) document.getElementById('scaleMonthlyDiscrepancyBills').innerText = discrepancyBills;

        const diffTxt = palletDiff > 0 ? `+${palletDiff}` : palletDiff;
        if(document.getElementById('monthlyPalletDiff')) document.getElementById('monthlyPalletDiff').innerText = diffTxt;
        if(document.getElementById('scaleMonthlyPalletDiff')) document.getElementById('scaleMonthlyPalletDiff').innerText = diffTxt;


        const tableBody = document.getElementById('monthlyRecordTableBody');
        const scaleTableBody = document.getElementById('scaleMonthlyRecordTableBody');

        if(monthlyRecords.length === 0) {
            const emptyMsg = '<tr><td colspan="10" class="text-center text-gray-500 py-10 text-lg">ไม่พบข้อมูลในเดือนที่เลือก</td></tr>';
            if(tableBody) tableBody.innerHTML = emptyMsg;
            if(scaleTableBody) scaleTableBody.innerHTML = emptyMsg;
            return;
        }

        let html = '';
        monthlyRecords.forEach(record => {
            const isDiscrepancy = record.expectedQty !== record.actualQty;
            const statusColor = isDiscrepancy ? 'text-red-600 font-bold bg-red-50' : 'text-green-600 font-bold';
            
            const diffValue = record.actualQty - record.expectedQty;
            const diffText = diffValue > 0 ? `+${diffValue}` : diffValue;
            const diffColor = diffValue !== 0 ? 'text-red-600 font-bold' : 'text-gray-400';
            
            let palletItemsHtml = '-';
            if (record.palletDetails && record.palletDetails.length > 0) {
                palletItemsHtml = `
                <table class="w-full text-xs text-left border border-gray-200 rounded overflow-hidden">
                    <tbody class="divide-y divide-gray-100">
                        ${record.palletDetails.map(p => `
                            <tr class="bg-gray-50 hover:bg-gray-100">
                                <td class="px-2 py-1 text-gray-700 w-3/4">${p.name}</td>
                                <td class="px-2 py-1 font-bold text-blue-700 text-right w-1/4 border-l border-gray-100">${p.qty}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>`;
            }

            html += `
                <tr class="hover:bg-blue-50 transition-colors border-b border-gray-100">
                    <td class="px-4 py-3 font-bold text-gray-800">${record.documentNumber}</td>
                    <td class="px-4 py-3 text-sm">${record.date}</td>
                    <td class="px-4 py-3 truncate max-w-xs text-sm" title="${record.customer_name}">${record.customer_name}</td>
                    <td class="px-4 py-3 text-sm text-purple-700 font-semibold">${record.plant_short_name || '-'}</td>
                    <td class="px-4 py-3 text-center text-sm">${record.checkerName}</td>
                    <td class="px-4 py-3 text-right">${record.expectedQty}</td>
                    <td class="px-4 py-3 text-right ${statusColor}">${record.actualQty}</td>
                    <td class="px-4 py-3 text-center ${diffColor}">${diffText}</td>
                    <td class="px-4 py-3 text-right font-bold text-green-700">${record.calculated_weight ? record.calculated_weight.toLocaleString() : '0'}</td>
                    <td class="px-4 py-2">${palletItemsHtml}</td>
                    <td class="px-4 py-3 text-center">
                        <button onclick="openImageModal('${record.imageBase64 || ''}')" class="text-blue-500 hover:text-blue-700 bg-blue-100 hover:bg-blue-200 p-2 rounded-lg transition-colors" title="ดูรูปภาพ">
                            <i class="fa-solid fa-image"></i>
                        </button>
                    </td>
                </tr>`;
        });
        if(tableBody) tableBody.innerHTML = html;
        if(scaleTableBody) scaleTableBody.innerHTML = html;

    } catch (error) {
        if (error.message === "Unauthorized") return;
        console.error("ไม่สามารถโหลดรายงานรายเดือนได้:", error);
    }
}

function renderScaleDashboard() {
    if (!globalAdminData) return;
    const db = globalAdminData; 
    
    const tableBody = document.getElementById('scaleRecordTableBody');
    const scalePageTableBody = document.getElementById('scalePageScaleRecordTableBody');
    
    if(db.length === 0) {
        const emptyMsg = `<tr><td colspan="8" class="text-center text-gray-500 py-10 text-lg">ไม่พบข้อมูลรถบรรทุก</td></tr>`;
        if(tableBody) tableBody.innerHTML = emptyMsg;
        if(scalePageTableBody) scalePageTableBody.innerHTML = emptyMsg;
        return;
    }

    let html = '';
    db.forEach(record => {
        const truck = record.truckDetail || {}; 
        const truckImg = truck.truck_image_base64;
        
        let scaleImgBtn = '';
        if (truckImg) {
            scaleImgBtn = `
                <button onclick="openImageModal('${truckImg}')" 
                        class="text-teal-700 hover:text-teal-900 bg-teal-100 hover:bg-teal-200 px-3 py-1.5 rounded-lg font-bold text-xs transition-colors shadow-sm inline-flex items-center">
                    <i class="fa-solid fa-image mr-1"></i> ดูรูปรถ
                </button>`;
        } else {
            scaleImgBtn = `<span class="text-gray-400 text-xs italic">ยังไม่มีรูป</span>`;
        }

        html += `
            <tr class="hover:bg-teal-50 transition-colors border-b border-gray-100">
                <td class="px-4 py-3 font-bold text-gray-800">${record.documentNumber}</td>
                <td class="px-4 py-3 font-bold text-teal-700 bg-teal-50 rounded-md">${truck.license_plate || '-'}</td>
                <td class="px-4 py-3 text-sm">${truck.weight_in_time || '-'}</td>
                <td class="px-4 py-3 text-right font-semibold text-blue-700">${truck.weight_in || '0'}</td>
                <td class="px-4 py-3 text-sm">${truck.weight_out_time || 'รอรถออก'}</td>
                <td class="px-4 py-3 text-right font-semibold text-purple-700">${truck.weight_out || '0'}</td>
                
                <!-- 👇 เพิ่มคอลัมน์แสดงน้ำหนักพาเลทที่รับมาจาก API ตรงนี้ 👇 -->
                <td class="px-4 py-3 text-right font-extrabold text-green-600">${truck.pallet_weight !== undefined ? truck.pallet_weight : '0'}</td>

                <td class="px-4 py-3 text-center">${scaleImgBtn}</td>
            </tr>`;
    });
    if(tableBody) tableBody.innerHTML = html;
    if(scalePageTableBody) scalePageTableBody.innerHTML = html;
}
function openImageModal(imgSrc) {
    if (!imgSrc) return;
    const modal = document.getElementById('imageViewerModal');
    const fullImg = document.getElementById('fullSizeImage');
    fullImg.src = imgSrc;
    modal.classList.remove('hidden');
    setTimeout(() => {
        fullImg.classList.remove('scale-95');
        fullImg.classList.add('scale-100');
    }, 10);
}

function closeImageModal() {
    const modal = document.getElementById('imageViewerModal');
    const fullImg = document.getElementById('fullSizeImage');
    fullImg.classList.remove('scale-100');
    fullImg.classList.add('scale-95');
    setTimeout(() => {
        modal.classList.add('hidden');
        fullImg.src = '';
    }, 300);
}

async function clearDatabase() {
    if(confirm("คำเตือน: คุณต้องการล้างข้อมูลประวัติทั้งหมด (รวมทั้งรูปรถ) ใช่หรือไม่? (การกระทำนี้จะลบข้อมูลออกจากฐานข้อมูลจริงและไม่สามารถกู้คืนได้)")) {
        try {
            const response = await fetchWithAuth('/api/clear', { method: 'DELETE' });
            const result = await response.json();
            
            if(result.success) {
                updateAdminDashboard();
                generateMonthlyReport();
                showCustomAlert('success', 'ล้างข้อมูลสำเร็จ', 'ลบประวัติในฐานข้อมูลระบบเรียบร้อยแล้ว');
            } else {
                showCustomAlert('warning', 'เกิดข้อผิดพลาด', result.message);
            }
        } catch(e) {
            if (e.message === "Unauthorized") return;
            showCustomAlert('warning', 'การเชื่อมต่อผิดพลาด', 'ไม่สามารถเชื่อมต่อฐานข้อมูลได้');
        }
    }
}

function showCustomAlert(type, title, message) {
    const modal = document.getElementById('customAlertModal');
    const icon = document.getElementById('alertIcon');
    const titleEl = document.getElementById('alertTitle');
    const messageEl = document.getElementById('alertMessage');
    const btn = document.getElementById('alertButton');

    titleEl.innerText = title; messageEl.innerText = message;

    if (type === 'warning') {
        icon.innerHTML = '<i class="fa-solid fa-circle-exclamation text-red-500"></i>';
        btn.className = 'w-full text-white py-3 rounded-lg font-bold text-lg transition-all active:scale-95 shadow-sm bg-red-600 hover:bg-red-700';
    } else if (type === 'info') {
        icon.innerHTML = '<i class="fa-solid fa-circle-info text-blue-500"></i>';
        btn.className = 'w-full text-white py-3 rounded-lg font-bold text-lg transition-all active:scale-95 shadow-sm bg-blue-600 hover:bg-blue-700';
    } else if (type === 'success') {
        icon.innerHTML = '<i class="fa-solid fa-circle-check text-green-500"></i>';
        btn.className = 'w-full text-white py-3 rounded-lg font-bold text-lg transition-all active:scale-95 shadow-sm bg-green-600 hover:bg-green-700';
    }
    modal.classList.remove('hidden');
}

function closeAlert() { 
    document.getElementById('customAlertModal').classList.add('hidden'); 
}