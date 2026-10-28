// ocr_host.cs - 常驻 OCR 服务 (Windows.Media.Ocr + GDI 截屏)
// 协议: 从 stdin 逐行读命令, 从 stdout 逐行回 JSON (UTF-8)
//   OCR  <x> <y> <w> <h> [scale]   截屏指定屏幕区域并识别
//   FILE <path> [scale]            识别图片文件
//   SAVE <path>                    把上一次 OCR 的原始截图存成 png
//   INFO                           返回环境信息
//   QUIT                           退出
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using Windows.Graphics.Imaging;
using Windows.Media.Ocr;
using Windows.Storage.Streams;

static class OcrHost
{
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int nIndex);

    static OcrEngine engine;
    static Bitmap lastBitmap;
    static StreamWriter outw;

    static void Main(string[] args)
    {
        try { SetProcessDPIAware(); } catch { }
        Console.OutputEncoding = new UTF8Encoding(false);
        outw = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false), 65536);
        outw.AutoFlush = true;
        var inw = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false), false, 65536);

        try { engine = OcrEngine.TryCreateFromLanguage(new Windows.Globalization.Language("zh-Hans-CN")); }
        catch (Exception e) { WriteJson(Err("engine init: " + e.Message)); return; }
        if (engine == null) { WriteJson(Err("no zh OCR engine")); return; }

        WriteJson("{\"ok\":true,\"ready\":true,\"maxdim\":" + OcrEngine.MaxImageDimension +
                  ",\"screen\":[" + GetSystemMetrics(0) + "," + GetSystemMetrics(1) + "]}");

        string line;
        while ((line = inw.ReadLine()) != null)
        {
            line = line.Trim();
            if (line.Length == 0) continue;
            string[] p = line.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            string cmd = p[0].ToUpperInvariant();
            if (cmd == "QUIT") break;
            try
            {
                if (cmd == "INFO") { WriteJson("{\"ok\":true,\"screen\":[" + GetSystemMetrics(0) + "," + GetSystemMetrics(1) + "]}"); }
                else if (cmd == "OCR")
                {
                    int x = int.Parse(p[1]), y = int.Parse(p[2]), w = int.Parse(p[3]), h = int.Parse(p[4]);
                    double scale = p.Length > 5 ? double.Parse(p[5], CultureInfo.InvariantCulture) : 2.0;
                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    Bitmap bmp = Grab(x, y, w, h);
                    lastBitmap = bmp;
                    long capMs = sw.ElapsedMilliseconds;
                    var res = Recognize(bmp, scale);
                    sw.Stop();
                    res = res.Insert(1, "\"cap\":" + capMs + ",\"ms\":" + sw.ElapsedMilliseconds + ",");
                    WriteJson(res);
                }
                else if (cmd == "FILE")
                {
                    double scale = p.Length > 2 ? double.Parse(p[2], CultureInfo.InvariantCulture) : 2.0;
                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    Bitmap bmp = new Bitmap(p[1]);
                    lastBitmap = bmp;
                    var res = Recognize(bmp, scale);
                    sw.Stop();
                    res = res.Insert(1, "\"ms\":" + sw.ElapsedMilliseconds + ",");
                    WriteJson(res);
                }
                else if (cmd == "SAVE")
                {
                    if (lastBitmap == null) { WriteJson(Err("no image")); }
                    else { lastBitmap.Save(p[1], ImageFormat.Png); WriteJson("{\"ok\":true}"); }
                }
                else WriteJson(Err("unknown cmd " + cmd));
            }
            catch (Exception e)
            {
                WriteJson(Err(e.GetType().Name + ": " + e.Message));
            }
        }
    }

    static Bitmap Grab(int x, int y, int w, int h)
    {
        if (w <= 0 || h <= 0) throw new ArgumentException("bad region");
        var bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb);
        using (var g = Graphics.FromImage(bmp))
        {
            g.CopyFromScreen(x, y, 0, 0, new Size(w, h), CopyPixelOperation.SourceCopy);
        }
        return bmp;
    }

    static string Recognize(Bitmap bmp, double scale)
    {
        int nw = Math.Max(1, (int)Math.Round(bmp.Width * scale));
        int nh = Math.Max(1, (int)Math.Round(bmp.Height * scale));
        int cap = (int)OcrEngine.MaxImageDimension;
        if (nw > cap) { double k = (double)cap / nw; nw = cap; nh = Math.Max(1, (int)(nh * k)); }
        if (nh > cap) { double k = (double)cap / nh; nh = cap; nw = Math.Max(1, (int)(nw * k)); }
        double eff = (double)nw / bmp.Width;

        SoftwareBitmap sb;
        using (var ms = new MemoryStream())
        {
            bmp.Save(ms, ImageFormat.Bmp);
            ms.Position = 0;
            using (var ras = new MemoryStream(ms.ToArray()))
            {
                var stream = ToWinRT(ras.ToArray());
                var decoderTask = BitmapDecoder.CreateAsync(stream);
                var decoder = Await(decoderTask);
                var transform = new BitmapTransform();
                transform.ScaledWidth = (uint)nw;
                transform.ScaledHeight = (uint)nh;
                transform.InterpolationMode = BitmapInterpolationMode.Linear;
                var task = decoder.GetSoftwareBitmapAsync(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied,
                                                          transform, ExifOrientationMode.IgnoreExifOrientation,
                                                          ColorManagementMode.DoNotColorManage);
                sb = Await(task);
            }
        }
        var ocr = Await(engine.RecognizeAsync(sb));

        var sbld = new StringBuilder();
        sbld.Append("{\"ok\":true,\"img\":[" + bmp.Width + "," + bmp.Height + "],\"scale\":" +
                    eff.ToString("0.###", CultureInfo.InvariantCulture) + ",\"lines\":[");
        bool first = true;
        foreach (var ln in ocr.Lines)
        {
            if (!first) sbld.Append(',');
            first = false;
            var r = ln.Words.Count > 0 ? Union(ln) : new Windows.Foundation.Rect(0, 0, 0, 0);
            sbld.Append("{\"t\":" + Q(ln.Text) + ",\"x\":" + X(r.X, eff) + ",\"y\":" + X(r.Y, eff) +
                        ",\"w\":" + X(r.Width, eff) + ",\"h\":" + X(r.Height, eff) + ",\"words\":[");
            for (int i = 0; i < ln.Words.Count; i++)
            {
                var wd = ln.Words[i];
                if (i > 0) sbld.Append(',');
                sbld.Append("{\"t\":" + Q(wd.Text) + ",\"x\":" + X(wd.BoundingRect.X, eff) + ",\"y\":" +
                            X(wd.BoundingRect.Y, eff) + ",\"w\":" + X(wd.BoundingRect.Width, eff) +
                            ",\"h\":" + X(wd.BoundingRect.Height, eff) + "}");
            }
            sbld.Append("]}");
        }
        sbld.Append("]}");
        return sbld.ToString();
    }

    static Windows.Foundation.Rect Union(Windows.Media.Ocr.OcrLine ln)
    {
        double x0 = double.MaxValue, y0 = double.MaxValue, x1 = double.MinValue, y1 = double.MinValue;
        foreach (var w in ln.Words)
        {
            var r = w.BoundingRect;
            if (r.X < x0) x0 = r.X;
            if (r.Y < y0) y0 = r.Y;
            if (r.X + r.Width > x1) x1 = r.X + r.Width;
            if (r.Y + r.Height > y1) y1 = r.Y + r.Height;
        }
        if (x0 > x1) return new Windows.Foundation.Rect(0, 0, 0, 0);
        return new Windows.Foundation.Rect(x0, y0, x1 - x0, y1 - y0);
    }

    static string X(double v, double eff)
    {
        return Math.Round(v / eff, 1).ToString("0.#", CultureInfo.InvariantCulture);
    }

    static string Q(string s)
    {
        var b = new StringBuilder("\"");
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': b.Append("\\\""); break;
                case '\\': b.Append("\\\\"); break;
                case '\n': b.Append("\\n"); break;
                case '\r': b.Append("\\r"); break;
                case '\t': b.Append("\\t"); break;
                default:
                    if (c < 0x20) b.Append("\\u" + ((int)c).ToString("x4"));
                    else b.Append(c);
                    break;
            }
        }
        b.Append('"');
        return b.ToString();
    }

    static string Err(string m) { return "{\"ok\":false,\"err\":" + Q(m) + "}"; }

    static void WriteJson(string s) { outw.WriteLine(s); }

    static IRandomAccessStream ToWinRT(byte[] data)
    {
        var ras = new InMemoryRandomAccessStream();
        var writer = new DataWriter(ras.GetOutputStreamAt(0));
        writer.WriteBytes(data);
        Await(writer.StoreAsync());
        Await(ras.FlushAsync());
        ras.Seek(0);
        return ras;
    }

    static T Await<T>(Windows.Foundation.IAsyncOperation<T> op) { return op.AsTask().GetAwaiter().GetResult(); }
    static void Await(Windows.Foundation.IAsyncAction op) { op.AsTask().GetAwaiter().GetResult(); }
}
