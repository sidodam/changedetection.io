import json
import os
from urllib.parse import urlparse

from loguru import logger
from changedetectionio.content_fetchers.base import Fetcher
from changedetectionio.content_fetchers.exceptions import PageUnloadable, Non200ErrorCodeReceived, EmptyReply, ScreenshotUnavailable

class fetcher(Fetcher):
    fetcher_description = "Playwright {}/Javascript".format(
        os.getenv("PLAYWRIGHT_BROWSER_TYPE", 'chromium').capitalize()
    )
    if os.getenv("PLAYWRIGHT_DRIVER_URL"):
        fetcher_description += " via '{}'".format(os.getenv("PLAYWRIGHT_DRIVER_URL"))

    browser_type = ''
    command_executor = ''

    # Configs for Proxy setup
    # In the ENV vars, is prefixed with "playwright_proxy_", so it is for example "playwright_proxy_server"
    playwright_proxy_settings_mappings = ['bypass', 'server', 'username', 'password']

    proxy = None

    def __init__(self, proxy_override=None, custom_browser_connection_url=None):
        super().__init__()

        self.browser_type = os.getenv("PLAYWRIGHT_BROWSER_TYPE", 'chromium').strip('"')

        if custom_browser_connection_url:
            self.browser_connection_is_custom = True
            self.browser_connection_url = custom_browser_connection_url
        else:
            # Fallback to fetching from system
            # .strip('"') is going to save someone a lot of time when they accidently wrap the env value
            self.browser_connection_url = os.getenv("PLAYWRIGHT_DRIVER_URL", 'ws://playwright-chrome:3000').strip('"')

        # If any proxy settings are enabled, then we should setup the proxy object
        proxy_args = {}
        for k in self.playwright_proxy_settings_mappings:
            v = os.getenv('playwright_proxy_' + k, False)
            if v:
                proxy_args[k] = v.strip('"')

        if proxy_args:
            self.proxy = proxy_args

        # allow per-watch proxy selection override
        if proxy_override:
            self.proxy = {'server': proxy_override}

        if self.proxy:
            # Playwright needs separate username and password values
            parsed = urlparse(self.proxy.get('server'))
            if parsed.username:
                self.proxy['username'] = parsed.username
                self.proxy['password'] = parsed.password

    def screenshot_step(self, step_n=''):
        screenshot = self.page.screenshot(type='jpeg', full_page=True, quality=int(os.getenv("SCREENSHOT_QUALITY", 72)))

        if self.browser_steps_screenshot_path is not None:
            destination = os.path.join(self.browser_steps_screenshot_path, 'step_{}.jpeg'.format(step_n))
            logger.debug(f"Saving step screenshot to {destination}")
            with open(destination, 'wb') as f:
                f.write(screenshot)

    def save_step_html(self, step_n):
        content = self.page.content()
        destination = os.path.join(self.browser_steps_screenshot_path, 'step_{}.html'.format(step_n))
        logger.debug(f"Saving step HTML to {destination}")
        with open(destination, 'w') as f:
            f.write(content)

    def run(self,
            url,
            timeout,
            request_headers,
            request_body,
            request_method,
            ignore_status_codes=False,
            current_include_filters=None,
            is_binary=False):

        from playwright.sync_api import sync_playwright
        import playwright._impl._errors
        from changedetectionio.content_fetchers import visualselector_xpath_selectors
        self.delete_browser_steps_screenshots()
        response = None

        with sync_playwright() as p:
            browser_type = getattr(p, self.browser_type)

            # Seemed to cause a connection Exception even tho I can see it connect
            # self.browser = browser_type.connect(self.command_executor, timeout=timeout*1000)
            # 60,000 connection timeout only
            browser = browser_type.connect_over_cdp(self.browser_connection_url, timeout=60000)

            # SOCKS5 with authentication is not supported (yet)
            # https://github.com/microsoft/playwright/issues/10567

            # Set user agent to prevent Cloudflare from blocking the browser
            # Use the default one configured in the App.py model that's passed from fetch_site_status.py
            context = browser.new_context(
                user_agent={k.lower(): v for k, v in request_headers.items()}.get('user-agent', None),
                proxy=self.proxy,
                # This is needed to enable JavaScript execution on GitHub and others
                bypass_csp=True,
                # Should be `allow` or `block` - sites like YouTube can transmit large amounts of data via Service Workers
                service_workers=os.getenv('PLAYWRIGHT_SERVICE_WORKERS', 'allow'),
                # Should never be needed
                accept_downloads=False
            )

            self.page = context.new_page()
            if len(request_headers):
                context.set_extra_http_headers(request_headers)

            # Listen for all console events and handle errors
            self.page.on("console", lambda msg: print(f"Playwright console: Watch URL: {url} {msg.type}: {msg.text} {msg.args}"))

            # Re-use as much code from browser steps as possible so its the same
            from changedetectionio.blueprint.browser_steps.browser_steps import steppable_browser_interface
            browsersteps_interface = steppable_browser_interface()
            browsersteps_interface.page = self.page

            response = browsersteps_interface.action_goto_url(value=url)
            self.headers = response.all_headers()

            if response is None:
                context.close()
                browser.close()
                logger.debug("Content Fetcher > Response object was none")
                raise EmptyReply(url=url, status_code=None)

            try:
                if self.webdriver_js_execute_code is not None and len(self.webdriver_js_execute_code):
                    browsersteps_interface.action_execute_js(value=self.webdriver_js_execute_code, selector=None)
            except playwright._impl._errors.TimeoutError as e:
                context.close()
                browser.close()
                # This can be ok, we will try to grab what we could retrieve
                pass
            except Exception as e:
                logger.debug(f"Content Fetcher > Other exception when executing custom JS code {str(e)}")
                context.close()
                browser.close()
                raise PageUnloadable(url=url, status_code=None, message=str(e))

            extra_wait = int(os.getenv("WEBDRIVER_DELAY_BEFORE_CONTENT_READY", 5)) + self.render_extract_delay
            self.page.wait_for_timeout(extra_wait * 1000)

            try:
                self.status_code = response.status
            except Exception as e:
                # https://github.com/dgtlmoon/changedetection.io/discussions/2122#discussioncomment-8241962
                logger.critical(f"Response from the browser/Playwright did not have a status_code! Response follows.")
                logger.critical(response)
                context.close()
                browser.close()
                raise PageUnloadable(url=url, status_code=None, message=str(e))

            if self.status_code != 200 and not ignore_status_codes:
                screenshot = self.page.screenshot(type='jpeg', full_page=True,
                                                  quality=int(os.getenv("SCREENSHOT_QUALITY", 72)))

                raise Non200ErrorCodeReceived(url=url, status_code=self.status_code, screenshot=screenshot)

            if len(self.page.content().strip()) == 0:
                context.close()
                browser.close()
                logger.debug("Content Fetcher > Content was empty")
                raise EmptyReply(url=url, status_code=response.status)

            # Run Browser Steps here
            if self.browser_steps_get_valid_steps():
                self.iterate_browser_steps()

            self.page.wait_for_timeout(extra_wait * 1000)

            # So we can find an element on the page where its selector was entered manually (maybe not xPath etc)
            if current_include_filters is not None:
                self.page.evaluate("var include_filters={}".format(json.dumps(current_include_filters)))
            else:
                self.page.evaluate("var include_filters=''")

            self.xpath_data = self.page.evaluate(
                "async () => {" + self.xpath_element_js.replace('%ELEMENTS%', visualselector_xpath_selectors) + "}")
            self.instock_data = self.page.evaluate("async () => {" + self.instock_data_js + "}")

            self.content = self.page.content()
            # Bug 3 in Playwright screenshot handling
            # Some bug where it gives the wrong screenshot size, but making a request with the clip set first seems to solve it
            # JPEG is better here because the screenshots can be very very large

            # Screenshots also travel via the ws:// (websocket) meaning that the binary data is base64 encoded
            # which will significantly increase the IO size between the server and client, it's recommended to use the lowest
            # acceptable screenshot quality here
            try:
                # The actual screenshot - this always base64 and needs decoding! horrible! huge CPU usage
                self.screenshot = self.page.screenshot(type='jpeg',
                                                       full_page=True,
                                                       quality=int(os.getenv("SCREENSHOT_QUALITY", 72)),
                                                       )
            except Exception as e:
                # It's likely the screenshot was too long/big and something crashed
                raise ScreenshotUnavailable(url=url, status_code=self.status_code)
            finally:
                context.close()
                browser.close()
