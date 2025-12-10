# ⚠️ System Limitations

### _Current Boundaries & Constraints_

While the e2e-Platform is powerful, it is important to understand its current limitations to ensure successful deployment and usage.

---

## 1. CAPTCHA Solving

- **Current State**: The system can _detect_ CAPTCHAs (Cloudflare, ReCaptcha, etc.) and pause.
- **Limitation**: It does **not** currently have a built-in auto-solver (like 2Captcha or Anti-Captcha integration).
- **Workaround**: Use the "Human-in-the-Loop" feature to manually solve the CAPTCHA when the system pauses, or integrate a 3rd party solver service.

## 2. Multi-Factor Authentication (MFA)

- **Current State**: Can handle standard username/password login.
- **Limitation**: Cannot automatically retrieve SMS or Email OTP codes.
- **Workaround**: The workflow supports a `PAUSE` signal. You can inject the OTP via the API `/resume` endpoint, but this requires custom implementation for each site.

## 3. Heavy Media Sites

- **Current State**: Optimized for text, e-commerce, and data-heavy sites.
- **Limitation**: Sites heavily reliant on WebGL, Canvas, or complex video streaming (e.g., 3D games) may have performance overhead due to the "Glass Box" inspection running on every frame.

## 4. Browser Fingerprinting

- **Current State**: Uses standard Playwright browsers.
- **Limitation**: Extremely sophisticated anti-bot systems (like Akamai Enterprise) may still detect the headless browser fingerprint.
- **Workaround**: Use the `use_premium_proxy` config to route traffic through residential IPs, which helps significantly.

## 5. Execution Time

- **Current State**: Semantic analysis takes time.
- **Limitation**: It is slower than a "dumb" scraper. Finding an element semantically takes ~50-200ms, whereas a hardcoded selector takes ~1ms.
- **Trade-off**: You trade raw speed for **reliability** and **resilience**.

---

## 🔮 Future Roadmap

- [ ] Integration with 2Captcha/CapSolver.
- [ ] Automated OTP retrieval via Email API.
- [ ] "Stealth Mode" plugin for Playwright to mask fingerprints.
