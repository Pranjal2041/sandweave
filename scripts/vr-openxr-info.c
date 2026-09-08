/* Read real runtime-recommended stereo dimensions without creating a session.
 * Compile in the sandbox with: cc vr-openxr-info.c -lopenxr_loader -o vr-openxr-info */
#include <openxr/openxr.h>
#include <stdio.h>

#define CHECK(call) do { XrResult result = (call); if (XR_FAILED(result)) { \
    fprintf(stderr, "%s: %d\n", #call, result); return 1; } } while (0)

int main(void)
{
    XrInstanceCreateInfo create = {XR_TYPE_INSTANCE_CREATE_INFO};
    snprintf(create.applicationInfo.applicationName, XR_MAX_APPLICATION_NAME_SIZE, "VR lab view query");
    create.applicationInfo.apiVersion = XR_CURRENT_API_VERSION;
    XrInstance instance;
    CHECK(xrCreateInstance(&create, &instance));
    XrSystemGetInfo info = {XR_TYPE_SYSTEM_GET_INFO};
    info.formFactor = XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    XrSystemId system;
    CHECK(xrGetSystem(instance, &info, &system));
    XrViewConfigurationView views[2] = {{XR_TYPE_VIEW_CONFIGURATION_VIEW}, {XR_TYPE_VIEW_CONFIGURATION_VIEW}};
    uint32_t count;
    CHECK(xrEnumerateViewConfigurationViews(instance, system, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,
                                          2, &count, views));
    printf("{\"view_count\":%u,\"views\":[", count);
    for (uint32_t i=0; i<count; i++) {
        printf("%s{\"width\":%u,\"height\":%u,\"samples\":%u}", i ? "," : "",
               views[i].recommendedImageRectWidth, views[i].recommendedImageRectHeight,
               views[i].recommendedSwapchainSampleCount);
    }
    printf("]}\n");
    CHECK(xrDestroyInstance(instance));
    return 0;
}
